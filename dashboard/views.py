from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods, require_safe
from django.core.paginator import Paginator

from users.models import EmailAccount, AccountGroup
from leads_data.models import DailySheet
from .models import GmailToken, CampaignRecord, EmailOpen
from warmup.models import WarmupCampaign
from drip_campaigns.models import SentDripEmail
from .forms import EmailAccountForm, CampaignForm, BulkCampaignForm
from .tasks import send_emails_chunk_celery_task, send_account_attach_notif_email
from .utilities import *
from django.db.models import F, Value, OuterRef, Subquery, Prefetch
from django.db.models.functions import Coalesce

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.utils.timezone import now, make_naive
from django.utils import timezone
from datetime import datetime
from django.db import transaction
from django.db.models import Sum

from google_secrets import *
from urllib.parse import quote_plus

import requests
import pytz
import os
import uuid
import csv

######################################## Campaign sending views

# Basic email regex for quick pre-validation (can be more robust if needed)
email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


@login_required
def campaign(request, email_account_id):
    email_account = get_object_or_404(EmailAccount, id=email_account_id, user=request.user)
    form = CampaignForm(user=request.user)

    if request.method == 'POST':
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
        post_data = request.POST.copy()  # Make mutable copy
        files_data = request.FILES

        # ✅ Fix: Normalize schedule datetime to string format acceptable to form
        raw_schedule = post_data.get("schedule_launch_datetime")
        if raw_schedule:
            try:
                dt_obj = datetime.fromisoformat(raw_schedule)
                if dt_obj.tzinfo:
                    dt_obj = make_naive(dt_obj)  # Remove timezone info
                post_data["schedule_launch_datetime"] = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                print("⛔ Failed to parse schedule datetime:", e)

        form = CampaignForm(post_data, files_data, user=request.user)


        if form.is_valid():
            email_subject = form.cleaned_data['email_subject']
            email_body = form.cleaned_data['email_body']
            file_upload = form.cleaned_data['file_upload']
            lower_limit_mc_number = form.cleaned_data['lower_limit_mc_number']
            upper_limit_mc_number = form.cleaned_data['upper_limit_mc_number']
            mc_number = form.cleaned_data['mc_number']
            targets_count = form.cleaned_data['targets_count']
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')
                
            scheduled_launch_datetime = form.cleaned_data.get('schedule_launch_datetime')
            skip_mc_numbers = form.cleaned_data.get("skip_mc_numbers")
            track_campaign = form.cleaned_data.get('track_campaign')

            power_units_comparison = form.cleaned_data.get('power_units_comparison')
            power_units_value = form.cleaned_data.get('power_units_value')
            drivers_comparison = form.cleaned_data.get('drivers_comparison')
            drivers_value = form.cleaned_data.get('drivers_value')
            status = form.cleaned_data.get('status')
            carrier_operation = form.cleaned_data.get('carrier_operation')
            cargo_classification_search = form.cleaned_data.get('cargo_classification_search')
            cargo_info_search = form.cleaned_data.get('cargo_info_search')

            leads = []
            debug_info = {}
            lead_source = ''

            if file_upload:
                leads = process_excel_file(file_upload, request.user)
                lead_source = 'Excel'
                debug_info['lead_source'] = 'Excel'
                debug_info['leads_count'] = len(leads)

            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                leads = get_leads_from_db(
                    request.user, mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
                    power_units_comparison=power_units_comparison, power_units_value=power_units_value,
                    drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, skip_mc_numbers=skip_mc_numbers,
                    cargo_classification_search_term=cargo_classification_search, cargo_info_search_term=cargo_info_search
                )
                lead_source = 'DB'
                debug_info['lead_source'] = 'DB'
                debug_info['leads_count'] = len(leads)

            if not leads:
                message = "❌ No valid leads found. Either the Excel is empty or filters didn't return any results."
                print("🛑", message)
                messages.error(request, message)
                if is_ajax:
                    return JsonResponse({
                        'success': False,
                        'message': message,
                        'debug': debug_info
                    }, status=400)
                return redirect('dashboard:index')

            # Remove duplicate emails
            seen_emails = set()
            unique_leads = []
            for lead in leads:
                email = lead.get("Email")
                if email and email not in seen_emails:
                    unique_leads.append(lead)
                    seen_emails.add(email)

            leads = unique_leads
            debug_info['unique_leads'] = len(leads)

            # Filters info for preview
            filter_data = {}
            for key in ['mc_number', 'targets_count', 'power_units_comparison', 'power_units_value',
                        'drivers_comparison', 'drivers_value', 'status', 'carrier_operation',
                        'cargo_classification_search', 'cargo_info_search']:
                val = locals().get(key)
                if val not in [None, '', 'None']:
                    filter_data[key] = val

            if is_ajax and not request.POST.get('confirm'):
                return JsonResponse({
                    'lead_count': len(leads),
                    'filters': filter_data,
                    'confirmed': False,
                    'success': True,
                    'debug': debug_info
                })

            if scheduled_launch_datetime:
                print("📅 Scheduler detected. Saving scheduled campaign...")
                CampaignRecord.objects.create(
                    subject = email_subject,
                    body = email_body,
                    leads_data = leads,
                    min_delay = min_delay,
                    max_delay = max_delay,
                    scheduled_launch_time = scheduled_launch_datetime,
                    launched_by = request.user,
                    sender_account = email_account,
                    total_recipients = len(leads),
                    sent_count = 0,
                    status = 'pending',
                    lead_source = 'Excel' if file_upload else 'DB',
                    track_campaign = track_campaign
                )
                pst_tz = pytz.timezone('Asia/Karachi')
                scheduled_time_pst = scheduled_launch_datetime.astimezone(pst_tz)
                success_message = f"✅ Campaign '{email_subject}' scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')}."

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'message': success_message,
                        'redirect_url': str(redirect('dashboard:index').url),
                        'debug': debug_info
                    })

                messages.success(request, success_message)
                return redirect('dashboard:index')
            else: # imidiate started campaign
                new_camp = CampaignRecord.objects.create(
                    subject = email_subject,
                    body = email_body,
                    leads_data = leads,
                    min_delay = min_delay,
                    max_delay = max_delay,
                    launched_by = request.user,
                    sender_account = email_account,
                    total_recipients = len(leads),
                    sent_count = 0,
                    status = 'processing',
                    lead_source =  'Excel' if file_upload else 'DB',
                    track_campaign = track_campaign
                )

                # Immediate send
                print(f"📤 Queuing email campaign to {len(leads)} leads for {email_account.email_address}")
                # Pass only the campaign id to Celery; the task will read leads and other params from DB
                send_emails_chunk_celery_task.delay(new_camp.id)
                email_account.last_used_at = now()
                email_account.save(update_fields=["last_used_at"])

                success_message = f"✅ Success! Emails are being sent for {email_account.email_address}."

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'message': success_message,
                        'redirect_url': str(redirect('dashboard:index').url),
                        'debug': debug_info
                    })

                messages.success(request, success_message)
                return redirect('dashboard:index')

        # Invalid form
        print("🛑 Form is invalid:", form.errors)
        if is_ajax:
            return JsonResponse({
                'success': False,
                'message': "Form is invalid.",
                'errors': form.errors
            }, status=400)
        return redirect('dashboard:index')

    return render(request, 'dashboard/campaign.html', {'form': form, 'email_account': email_account})


@login_required
@require_http_methods(["GET", "POST"])
def bulk_campaign_step1(request):
    
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    form = BulkCampaignForm(user=request.user)

    if request.method == 'POST' and 'submit_leads' in request.POST:
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            
            campaign_key = str(uuid.uuid4())
            cache_key = f"bulk_leads_{request.user.id}_{campaign_key}"

            file_upload = form.cleaned_data['file_upload']
            mc_number = form.cleaned_data['mc_number']
            lower_limit_mc_number = form.cleaned_data['lower_limit_mc_number']
            upper_limit_mc_number = form.cleaned_data['upper_limit_mc_number']
            targets_count = form.cleaned_data['targets_count']
            skip_mc_numbers = form.cleaned_data.get("skip_mc_numbers")

            # Extra filters from the form
            power_units_comparison = form.cleaned_data.get('power_units_comparison')
            power_units_value = form.cleaned_data.get('power_units_value')
            drivers_comparison = form.cleaned_data.get('drivers_comparison')
            drivers_value = form.cleaned_data.get('drivers_value')
            status = form.cleaned_data.get('status')
            carrier_operation = form.cleaned_data.get('carrier_operation')
            cargo_classification_search = form.cleaned_data.get('cargo_classification_search')
            cargo_info_search = form.cleaned_data.get('cargo_info_search')

            leads = []
            if file_upload:
                lead_source = 'Excel'
                leads = process_excel_file(file_upload, request.user)
            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                lead_source = 'DB'
                leads = get_leads_from_db(
                    request.user, mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
                    power_units_comparison=power_units_comparison, power_units_value=power_units_value, 
                    drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, skip_mc_numbers=skip_mc_numbers,
                    cargo_classification_search_term=cargo_classification_search, cargo_info_search_term=cargo_info_search
                )

            if not leads:
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:bulk_campaign')
            
            # Before setting the cache, delete the old one
            cache.delete(cache_key)

            if lead_source == "Excel":
                # Save file in tmp storage
                tmp_path = save_temp_file(file_upload)
                cache_data = {
                    'lead_source': 'Excel',
                    'file_path': tmp_path,
                    'leads_available': len(leads)
                }
            else:
                cache_data = {
                    'lead_source': 'DB',
                    'params': {
                        'user': request.user,
                        'starting_mc_number': mc_number,
                        'targets_count': targets_count,
                        'lower_limit_mc_number': lower_limit_mc_number,
                        'upper_limit_mc_number': upper_limit_mc_number,
                        'power_units_comparison': power_units_comparison,
                        'power_units_value': power_units_value,
                        'drivers_comparison': drivers_comparison,
                        'drivers_value': drivers_value,
                        'status': status,
                        'carrier_operation': carrier_operation,
                        'skip_mc_numbers': skip_mc_numbers,
                        'cargo_classification_search_term': cargo_classification_search,
                        'cargo_info_search_term': cargo_info_search
                    },
                    'leads_available': len(leads)
                }

            # not storing all the leads in the cache so it doesnt break
            cache.set(cache_key, cache_data, timeout=3600)
            return redirect('dashboard:bulk_campaign_step2', campaign_key=campaign_key)

        else:
            messages.error(request, f"Errors: {form.errors}")
            # return redirect(request.path)
            print(form.errors)
        
        
    return render(request, 'dashboard/bulk_campaign_step1.html', {
        'form': form,
        'email_accounts': email_accounts,
        'email_accounts_count': email_accounts_count,
    })



# @login_required
# @require_http_methods(["GET", "POST"])
# def bulk_campaign_step2(request, campaign_key):
    
#     cache_key = f"bulk_leads_{request.user.id}_{campaign_key}"
#     cached_data = cache.get(cache_key)
#     leads_count = cached_data.get('leads_available', 0) if cached_data else 0
#     email_accounts_count = EmailAccount.objects.filter(user=request.user).count()
    
#     # Check if leads are cached from Step 1
#     if not cached_data:
#         messages.error(request, "Lead data not found. Please start over.")
#         return redirect('dashboard:bulk_campaign')
    
#     # --- UPDATED: Fetch Groups ---
#     account_groups = AccountGroup.objects.filter(
#         user=request.user, 
#         email_accounts__isnull=False
#     ).distinct().prefetch_related('email_accounts')
    
#     form = BulkCampaignForm(user=request.user)

#     if request.method == 'POST' and 'submit_allocation' in request.POST:
        
#         total_leads = cached_data['leads_available'] if cached_data and 'leads_available' in cached_data else 0
#         form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)
#         if not cached_data:
#             messages.error(request, "Lead data not found. Please start over.")
#             return redirect('dashboard:bulk_campaign')
        
#         lead_source = cached_data['lead_source']
#         refetched_leads = [] # cause they were fetched once before in the first step

#         if lead_source == "Excel":
#             file_path = cached_data['file_path']
#             with open(file_path, 'rb') as f:
#                 refetched_leads = process_excel_file(f, request.user)

#         elif lead_source == "DB":
#             params = cached_data['params']
#             refetched_leads = get_leads_from_db(**params)
        
#         # If refetch failed or returned no leads, surface a form error and re-render Step 2
#         if not refetched_leads:
#             form.add_error(None, "No leads could be reloaded. Please restart the bulk campaign flow and try again.")
#             return render(request, 'dashboard/bulk_campaign_step2.html', {
#                 'form': form,
#                 'account_groups': account_groups,
#                 'email_accounts_count': email_accounts_count,
#                 'leads_ready': bool(cached_data),
#                 'leads_count': 0,
#             })

#         if form.is_valid():

#             leads = refetched_leads
#             email_subject = form.cleaned_data.get('email_subject')
#             email_body = form.cleaned_data.get('email_body')
#             select_all = form.cleaned_data.get('select_all')
#             min_delay = form.cleaned_data.get('min_delay')
#             max_delay = form.cleaned_data.get('max_delay')
                
#             scheduled_launch_datetime = form.cleaned_data.get('schedule_launch_datetime')
#             lead_source = cached_data.get('lead_source')
#             track_campaign = form.cleaned_data.get('track_campaign')

#             # --- NEW GROUP ALLOCATION LOGIC ---
#             group_lead_counts_map = {}

#             if select_all:
#                 # Filter groups that actually have accounts
#                 valid_groups = [g for g in account_groups if g.email_accounts.exists()]
                
#                 if not valid_groups:
#                     form.add_error(None, "No valid groups found (groups must contain at least one email account).")
#                     return render(request, 'dashboard/bulk_campaign_step2.html', {
#                         'form': form,
#                         'account_groups': account_groups,
#                         'email_accounts_count': email_accounts_count,
#                         'leads_ready': bool(cached_data),
#                         'leads_count': len(leads),
#                     })

#                 # Auto-Calculate leads per group
#                 total_valid_groups = len(valid_groups)
#                 base_count = len(leads) // total_valid_groups
#                 remainder = len(leads) % total_valid_groups

#                 for i, group in enumerate(valid_groups):
#                     count = base_count + (1 if i < remainder else 0)
#                     group_lead_counts_map[group] = count

#             else:
#                 # Manual Allocation: Read from POST data based on Group ID
#                 selected_group_ids = request.POST.getlist('selected_groups')
                
#                 for group_id in selected_group_ids:
#                     try:
#                         group = AccountGroup.objects.get(id=group_id, user=request.user)
#                         if not group.email_accounts.exists():
#                             continue

#                         count_str = request.POST.get(f'leads_for_group_{group_id}', '0')
#                         count = int(count_str)
                        
#                         if count > 0:
#                             group_lead_counts_map[group] = count
                            
#                     except (AccountGroup.DoesNotExist, ValueError):
#                         continue

#             # Flatten Group Distribution to Account Distribution
#             final_account_lead_map = distribute_leads_via_groups(leads, group_lead_counts_map)

#             # --- CAMPAIGN PROCESSING (Logic Preserved) ---

#             def start_campaign_processing():
#                 scheduled_campaigns = []
#                 immediate_campaigns = []
#                 accounts_to_update = []

#                 with transaction.atomic():
#                     # Iterate over the flattened account map
#                     for account, assigned_leads in final_account_lead_map.items():
#                         if assigned_leads:
#                             if scheduled_launch_datetime:
#                                 # Prepare scheduled campaign record
#                                 scheduled_campaigns.append(CampaignRecord(
#                                     subject=email_subject,
#                                     body=email_body,
#                                     leads_data=assigned_leads,
#                                     min_delay=min_delay,
#                                     max_delay=max_delay,
#                                     scheduled_launch_time=scheduled_launch_datetime, # Already UTC from form.clean()
#                                     launched_by=request.user,
#                                     sender_account=account,
#                                     total_recipients=len(assigned_leads),
#                                     sent_count=0,
#                                     status='pending',
#                                     lead_source=lead_source,
#                                     track_campaign=track_campaign
#                                 ))
#                                 print(f"Scheduled bulk campaign for {account.email_address} with {len(assigned_leads)} leads.")
#                             else:
#                                 # Prepare immediate campaign record
#                                 immediate_campaigns.append(CampaignRecord(
#                                     subject=email_subject,
#                                     body=email_body,
#                                     leads_data=assigned_leads,
#                                     min_delay=min_delay,
#                                     max_delay=max_delay,
#                                     launched_by=request.user,
#                                     sender_account=account,
#                                     total_recipients=len(assigned_leads),
#                                     sent_count=0,
#                                     status='processing',
#                                     lead_source=lead_source,
#                                     track_campaign=track_campaign
#                                 ))
#                                 print(f"Queuing immediate bulk email campaign to {len(assigned_leads)} leads for {account.email_address}")
                                
#                                 # Mark account for updating last_used_at
#                                 account.last_used_at = now()
#                                 accounts_to_update.append(account)

#                     # Bulk create scheduled campaigns
#                     if scheduled_campaigns:
#                         CampaignRecord.objects.bulk_create(scheduled_campaigns)
                    
#                     # Bulk create immediate campaigns and get their IDs
#                     created_immediate_campaigns = []
#                     if immediate_campaigns:
#                         created_immediate_campaigns = CampaignRecord.objects.bulk_create(immediate_campaigns)
                    
#                     # Update email accounts' last_used_at in bulk
#                     if accounts_to_update:
#                         EmailAccount.objects.bulk_update(accounts_to_update, ['last_used_at'])
                    
#                     # Queue immediate campaigns for processing
#                     immediate_campaign_count = 0
#                     # We iterate through immediate_campaigns list to match created objects order
#                     for campaign in created_immediate_campaigns:
#                         send_emails_chunk_celery_task.delay(campaign.id)
#                         immediate_campaign_count += 1

#                 return len(scheduled_campaigns), len(created_immediate_campaigns)

#             scheduled_count, immediate_count = start_campaign_processing()
#             cache.delete(cache_key) # Clean up cache

#             # Display messages using PST (Pakistan Standard Time)
#             pst_tz = pytz.timezone('Asia/Karachi')
#             scheduled_time_pst = scheduled_launch_datetime.astimezone(pst_tz) if scheduled_launch_datetime else None

#             if scheduled_count > 0 and immediate_count > 0:
#                 messages.success(request, f"🎉 {scheduled_count} campaigns scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')} and {immediate_count} campaigns launched immediately!")
#             elif scheduled_count > 0:
#                 messages.success(request, f"🎉 {scheduled_count} bulk campaigns scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')}!")
#             elif immediate_count > 0:
#                 messages.success(request, f"🎉 Bulk Campaigns launched successfully! Emails are being sent!")
#             else:
#                 messages.info(request, "No campaigns were launched or scheduled.")

#             # ✅ cleanup temp file after processing
#             if cached_data.get("lead_source") == "Excel":
#                 file_path = cached_data.get("file_path")
#                 if file_path and os.path.exists(file_path):
#                     os.remove(file_path)

#             return redirect('dashboard:index')

#         else:
#             return render(request, 'dashboard/bulk_campaign_step2.html', {
#                 'form': form,  # bound form with errors
#                 'account_groups': account_groups,
#                 'email_accounts_count': email_accounts_count,
#                 'leads_ready': bool(cached_data),
#                 'leads_count': leads_count,
#             })
        

#     return render(request, 'dashboard/bulk_campaign_step2.html', {
#         'form': form,
#         'account_groups': account_groups,
#         'email_accounts_count': email_accounts_count,
#         'leads_ready': bool(cached_data),
#         'leads_count': leads_count,
#     })


@login_required
@require_http_methods(["GET", "POST"])
def bulk_campaign_step2(request, campaign_key):
    
    cache_key = f"bulk_leads_{request.user.id}_{campaign_key}"
    cached_data = cache.get(cache_key)
    leads_count = cached_data.get('leads_available', 0) if cached_data else 0
    
    # Check if leads are cached from Step 1
    if not cached_data:
        messages.error(request, "Lead data not found. Please start over.")
        return redirect('dashboard:bulk_campaign')
    
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    form = BulkCampaignForm(user=request.user)

    if request.method == 'POST' and 'submit_allocation' in request.POST:
        
        total_leads = cached_data['leads_available'] if cached_data and 'leads_available' in cached_data else 0
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)
        if not cached_data:
            messages.error(request, "Lead data not found. Please start over.")
            return redirect('dashboard:bulk_campaign')
        
        lead_source = cached_data['lead_source']
        refetched_leads = [] # cause they were fetched once before in the first step

        if lead_source == "Excel":
          file_path = cached_data['file_path']
          with open(file_path, 'rb') as f:
              refetched_leads = process_excel_file(f, request.user)

        elif lead_source == "DB":
            params = cached_data['params']
            refetched_leads = get_leads_from_db(**params)
        
        # If refetch failed or returned no leads, surface a form error and re-render Step 2
        if not refetched_leads:
            form.add_error(None, "No leads could be reloaded. Please restart the bulk campaign flow and try again.")
            return render(request, 'dashboard/bulk_campaign_step2.html', {
                'form': form,
                'email_accounts': email_accounts,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'leads_count': 0,
            })

        if form.is_valid():

            leads = refetched_leads
            email_subject = form.cleaned_data.get('email_subject')
            email_body = form.cleaned_data.get('email_body')
            select_all = form.cleaned_data.get('select_all')
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')
                
            scheduled_launch_datetime = form.cleaned_data.get('schedule_launch_datetime')
            lead_source = cached_data.get('lead_source')
            track_campaign = form.cleaned_data.get('track_campaign')

            selected_account_ids = request.POST.getlist('selected_accounts')
            account_lead_map = {}
            total_requested_leads = 0

            if select_all:
                # ✅ Only take the accounts that are CHECKED in the form
                selected_ids = request.POST.getlist('selected_accounts')
                accounts = EmailAccount.objects.filter(user=request.user, id__in=selected_ids)

                if not accounts.exists():
                    form.add_error(None, "No email accounts found for your user.")
                    return render(request, 'dashboard/bulk_campaign_step2.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'leads_count': len(leads),
                    })

                # ✅ Auto-distribute leads among the checked accounts only
                account_lead_map = distribute_leads_among_accounts(leads, list(accounts))

            else:
                for account_id in selected_account_ids:
                    try:
                        num_leads = int(request.POST.get(f'emails_for_account_{account_id}', '0'))
                        if num_leads < 1:
                            continue

                        account = EmailAccount.objects.get(id=account_id, user=request.user)
                        account_lead_map[account] = num_leads
                        total_requested_leads += num_leads
                    except (ValueError, EmailAccount.DoesNotExist):
                        continue

                if total_requested_leads != len(leads):
                    form.add_error(None, f"Total assigned leads ({total_requested_leads}) must match total available ({len(leads)}).")
                    return render(request, 'dashboard/bulk_campaign_step2.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'leads_count': len(leads),
                    })

            if not select_all:
                lead_index = 0
                updated_map = {}

                for account, count in account_lead_map.items():
                    if not isinstance(count, int):
                        try:
                            count = int(count[0]) if isinstance(count, list) else int(count)
                        except (ValueError, TypeError):
                            form.add_error(None, f"Invalid lead count for account {account}")
                            return render(request, 'dashboard/bulk_campaign_step2.html', {
                                'form': form,
                                'email_accounts': email_accounts,
                                'email_accounts_count': email_accounts_count,
                                'leads_ready': bool(cached_data),
                                'leads_count': len(leads),
                            })

                    updated_map[account] = leads[lead_index:lead_index + count]
                    lead_index += count

                account_lead_map = updated_map

            def start_campaign_processing():
                scheduled_campaigns = []
                immediate_campaigns = []
                accounts_to_update = []

                with transaction.atomic():
                    # Prepare campaign records for bulk creation
                    for account, assigned_leads in account_lead_map.items():
                        if assigned_leads:
                            if scheduled_launch_datetime:
                                # Prepare scheduled campaign record
                                scheduled_campaigns.append(CampaignRecord(
                                    subject=email_subject,
                                    body=email_body,
                                    leads_data=assigned_leads,
                                    min_delay=min_delay,
                                    max_delay=max_delay,
                                    scheduled_launch_time=scheduled_launch_datetime, # Already UTC from form.clean()
                                    launched_by=request.user,
                                    sender_account=account,
                                    total_recipients=len(assigned_leads),
                                    sent_count=0,
                                    status='pending',
                                    lead_source=lead_source,
                                    track_campaign=track_campaign
                                ))
                                print(f"Scheduled bulk campaign for {account.email_address} with {len(assigned_leads)} leads.")
                            else:
                                # Prepare immediate campaign record
                                immediate_campaigns.append(CampaignRecord(
                                    subject=email_subject,
                                    body=email_body,
                                    leads_data=assigned_leads,
                                    min_delay=min_delay,
                                    max_delay=max_delay,
                                    launched_by=request.user,
                                    sender_account=account,
                                    total_recipients=len(assigned_leads),
                                    sent_count=0,
                                    status='processing',
                                    lead_source=lead_source,
                                    track_campaign=track_campaign
                                ))
                                print(f"Queuing immediate bulk email campaign to {len(assigned_leads)} leads for {account.email_address}")
                                
                                # Mark account for updating last_used_at
                                account.last_used_at = now()
                                accounts_to_update.append(account)

                    # Bulk create scheduled campaigns
                    if scheduled_campaigns:
                        CampaignRecord.objects.bulk_create(scheduled_campaigns)
                    
                    # Bulk create immediate campaigns and get their IDs
                    created_immediate_campaigns = []
                    if immediate_campaigns:
                        created_immediate_campaigns = CampaignRecord.objects.bulk_create(immediate_campaigns)
                    
                    # Update email accounts' last_used_at in bulk
                    if accounts_to_update:
                        EmailAccount.objects.bulk_update(accounts_to_update, ['last_used_at'])
                    
                    # Queue immediate campaigns for processing
                    immediate_campaign_count = 0
                    for i, (account, assigned_leads) in enumerate([(acc, leads) for acc, leads in account_lead_map.items() if leads and not scheduled_launch_datetime]):
                        campaign = created_immediate_campaigns[immediate_campaign_count]
                        # Queue the kicker task by campaign id only
                        send_emails_chunk_celery_task.delay(campaign.id)
                        immediate_campaign_count += 1

                return len(scheduled_campaigns), len(created_immediate_campaigns)

            scheduled_count, immediate_count = start_campaign_processing()
            cache.delete(cache_key) # Clean up cache

            # Display messages using PST (Pakistan Standard Time)
            pst_tz = pytz.timezone('Asia/Karachi')
            scheduled_time_pst = scheduled_launch_datetime.astimezone(pst_tz) if scheduled_launch_datetime else None

            if scheduled_count > 0 and immediate_count > 0:
                messages.success(request, f"🎉 {scheduled_count} campaigns scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')} and {immediate_count} campaigns launched immediately!")
            elif scheduled_count > 0:
                messages.success(request, f"🎉 {scheduled_count} bulk campaigns scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')}!")
            elif immediate_count > 0:
                messages.success(request, f"🎉 Bulk Campaigns launched successfully! Emails are being sent!")
            else:
                messages.info(request, "No campaigns were launched or scheduled.")

            # ✅ cleanup temp file after processing
            if cached_data.get("lead_source") == "Excel":
                file_path = cached_data.get("file_path")
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)

            return redirect('dashboard:index')

        else:
            return render(request, 'dashboard/bulk_campaign_step2.html', {
                'form': form,  # bound form with errors
                'email_accounts': email_accounts,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'leads_count': leads_count,
            })
        

    return render(request, 'dashboard/bulk_campaign_step2.html', {
        'form': form,
        'email_accounts': email_accounts,
        'email_accounts_count': email_accounts_count,
        'leads_ready': bool(cached_data),
        'leads_count': leads_count,
    })



@require_safe
def track_open(request, unique_identifier):
    try:
        with transaction.atomic():
            email_log = (
                EmailOpen.objects
                .select_for_update()  # Locks row until transaction ends
                .get(unique_identifier=unique_identifier)
            )

            # Detect suspicious proxy/preload hits
            ua = request.META.get('HTTP_USER_AGENT', '').lower()
            ip = request.META.get('REMOTE_ADDR', '')

            suspicious_patterns = [
                'applemail',        # Apple Mail Privacy Protection
                'googleimageproxy', # Gmail image proxy
                'outlook',          # Outlook image cache proxy
                'yahoo',            # Yahoo Mail proxy
                'samsung',          # Samsung Email client
            ]

            if any(pattern in ua for pattern in suspicious_patterns):
                print(f"⚠️ Suspicious open detected for {email_log.recipient_email} (UA: {ua}, IP: {ip})")
                return gif_response()  # Exit without increment

            # If real open (idempotent due to DB row lock)
            if not email_log.is_opened:
                campaign = email_log.campaign
                campaign.open_rate = F('open_rate') + 1
                campaign.save(update_fields=['open_rate'])

                email_log.is_opened = True
                email_log.save(update_fields=['is_opened'])


    except EmailOpen.DoesNotExist as e:
        print(
              f"The tracking pixel was hit with an unknown unique_identifier:\n\n"
              f"{unique_identifier}\n\n"
              f"IP: {request.META.get('REMOTE_ADDR', '')}\n"
              f"User-Agent: {request.META.get('HTTP_USER_AGENT', '')}"
          )

    return gif_response()



######################################## Email accounts creation and dashboard views


@login_required
def campaign_records(request):
    
    # Filter campaigns for the current user with a 'launched' status
    campaign_list = CampaignRecord.objects.filter(
        launched_by=request.user,
        track_campaign=True
    ).order_by('-launch_time')
    
    # Paginate the results, 20 cords per page
    paginator = Paginator(campaign_list, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj
    }
    
    return render(request, 'dashboard/campaign_records.html', context)


class Echo:
    """An object that implements just the write method of the file-like interface."""
    def write(self, value):
        return value

@login_required
def export_email_opens(request):
    
    queryset = EmailOpen.objects.filter(
        launched_by=request.user, is_opened=True
    ).values_list('mc_number', 'legal_name', 'recipient_email').iterator()

    # 2. Determine Date for Filename "Email_Opens_till_{date}"
    latest_open = EmailOpen.objects.filter(launched_by=request.user).select_related('campaign').order_by('-timestamp').first()
    date_str = now().date().isoformat()
    
    if latest_open:
        if latest_open.campaign:
            # Case A: Campaign still exists -> Use launch time
            target_time = latest_open.campaign.launch_time or latest_open.campaign.scheduled_launch_time
            if target_time:
                date_str = target_time.date().isoformat()
        else:
            # Case B: Campaign was deleted by Celery -> Use the open timestamp
            if latest_open.timestamp:
                date_str = latest_open.timestamp.date().isoformat()

    # 3. Define the Generator
    def stream_csv():
        buffer = Echo()
        writer = csv.writer(buffer)
        
        yield writer.writerow(["MC Number", "Legal Name", "Email"])

        for row in queryset:
            yield writer.writerow(row)

    # 4. Construct Streaming Response
    response = StreamingHttpResponse(stream_csv(), content_type="text/csv")
    response['Content-Disposition'] = f'attachment; filename="Email_Opens__till_{date_str}.csv"'
    
    return response


@login_required
def delete_campaign(request, cmpn_id):
    
    campaign = get_object_or_404(CampaignRecord, id=cmpn_id, launched_by=request.user)
    campaign.delete()
    return redirect("dashboard:campaign_records")


@login_required
def index(request):
    latest_campaign_id_subquery = Subquery(
        CampaignRecord.objects.filter(sender_account=OuterRef('id'))
        .order_by('-launch_time')
        .values('id')[:1]
    )
    latest_campaign_status_subquery = Subquery(
        CampaignRecord.objects.filter(sender_account=OuterRef('id'))
        .order_by('-launch_time')
        .values('status')[:1]
    )
    latest_warmup_status_subquery = Subquery(
        WarmupCampaign.objects.filter(sender_account=OuterRef('id'))
        .order_by('-created_at')
        .values('status')[:1]
    )

    email_accounts_queryset = EmailAccount.objects.filter(user=request.user).order_by('-last_used_at').annotate(
        _latest_campaign_id=latest_campaign_id_subquery,
        last_campaign_status=Coalesce(latest_campaign_status_subquery, Value('N/A')),
        latest_warmup_status=Coalesce(latest_warmup_status_subquery, Value('N/A'))
    )

    prefetched_campaigns = Prefetch(
        'campaigns',
        queryset=CampaignRecord.objects.filter(id__in=Subquery(email_accounts_queryset.values('_latest_campaign_id'))),
        to_attr='_latest_campaign_obj'
    )

    email_accounts = email_accounts_queryset.prefetch_related(prefetched_campaigns)

    for account in email_accounts:
        account.is_gmail = account.email_address.lower().endswith('@gmail.com')
        account.is_connected = hasattr(account, 'gmail_token') and account.gmail_token is not None
        account.latest_campaign = account._latest_campaign_obj[0] if hasattr(account, '_latest_campaign_obj') and account._latest_campaign_obj else None

        # New block to add scheduled time for pending campaigns
        if account.latest_campaign and account.latest_campaign.status == "pending":
            account.scheduled_launch_time_display = account.latest_campaign.scheduled_launch_time
        else:
            account.scheduled_launch_time_display = None

    user_subscription = getattr(request.user, 'subscription', None)
    is_warmup_eligible = (
        user_subscription is not None and
        user_subscription.status == "active" and
        user_subscription.type in ("warmup", "premium")
    )
    is_unibox_eligible = (
        user_subscription is not None and
        user_subscription.status == "active" and
        user_subscription.type in ("unibox", "premium")
    )

    # to toggle the display for the stop all campaigns button
    active_campaigns = False
    if CampaignRecord.objects.filter(sender_account__user=request.user, status__in=['processing']).exists():
        active_campaigns = True

    context = {
        "email_accounts": email_accounts,
        "is_warmup_eligible": is_warmup_eligible,
        "is_unibox_eligible": is_unibox_eligible,
        "active_campaigns": active_campaigns
    }
    return render(request, 'dashboard/index.html', context)


@login_required
@require_http_methods(["POST"])
def emergency_stop(request, email_account_id):
    """
    Soft-cancels the latest 'processing' campaign for the given email account
    by marking its status as 'cancelled'. The Celery task will detect this and exit cleanly.
    """
    email_account = get_object_or_404(EmailAccount, id=email_account_id)

    # Ensure the logged-in user owns this account
    if email_account.user != request.user:
        messages.error(request, "You do not have permission to manage this email account.")
        return redirect("dashboard:index")

    # Get the latest campaign that is currently 'processing' or 'pending'
    latest_processing_campaign = CampaignRecord.objects.filter(
        sender_account=email_account,
        status__in=['processing', 'pending']
    ).order_by('-id').first()

    if latest_processing_campaign:
        latest_processing_campaign.status = 'cancelled'
        latest_processing_campaign.is_campaign_dispatched = False
        latest_processing_campaign.save(update_fields=['status', 'is_campaign_dispatched'])

        messages.success(
            request,
            f"Campaign '{latest_processing_campaign.subject}' has been cancelled."
        )

    else:
        messages.info(request, f"No active campaign found for {email_account.email_address} to stop.")

    return redirect("dashboard:index")


@login_required
def stop_all_campaigns(request):
    user = request.user
    active_campaigns = CampaignRecord.objects.filter(sender_account__user=user, status='processing').update(status='cancelled', is_campaign_dispatched = False)
    messages.success(request, f"All active campaigns ({active_campaigns}) have been cancelled.")
    return redirect("dashboard:index")


@login_required
@require_http_methods(["POST"])
def resume_stopped(request, email_account_id):
    """
    Resumes a stopped campaign. If the campaign was originally a scheduled campaign
    that was cancelled, it will be rescheduled for its original launch time.
    Otherwise, it will be launched immediately.
    """
    email_account = get_object_or_404(EmailAccount, id=email_account_id)

    # Ensure the logged-in user owns this account
    if email_account.user != request.user:
        messages.error(request, "You do not have permission to manage this email account.")
        return redirect("dashboard:index")
    
    # Get the latest campaign that is currently 'cancelled' (stopped)
    latest_cancelled_campaign = CampaignRecord.objects.filter(
        sender_account=email_account,
        status='cancelled'
    ).order_by('-id').first()

    if latest_cancelled_campaign:
        # Check if the campaign was a scheduled one that was cancelled before launch.
        # We also need to make sure we don't reschedule campaigns from the past
        is_scheduled = (latest_cancelled_campaign.scheduled_launch_time and 
                        latest_cancelled_campaign.scheduled_launch_time > now())

        if is_scheduled:
            # Revert status to pending and reschedule the Celery task
            latest_cancelled_campaign.status = 'pending'
            latest_cancelled_campaign.is_campaign_dispatched = False
            latest_cancelled_campaign.save(update_fields=['status', 'is_campaign_dispatched'])
            
            send_emails_chunk_celery_task.apply_async(
                args=(latest_cancelled_campaign.id,),
                eta=latest_cancelled_campaign.scheduled_launch_time
            )

            messages.success(
                request,
                f"Campaign '{latest_cancelled_campaign.subject}' has been rescheduled to its original launch time."
            )
        else:
            # Revert status to processing and launch immediately
            latest_cancelled_campaign.status = 'processing'
            latest_cancelled_campaign.is_campaign_dispatched = False
            latest_cancelled_campaign.save(update_fields=['status', 'is_campaign_dispatched'])

            # Recall the celery worker for that stopped campaign
            send_emails_chunk_celery_task.delay(latest_cancelled_campaign.id)

            messages.success(
                request,
                f"Campaign '{latest_cancelled_campaign.subject}' has been resumed successfully."
            )
    else:
        messages.info(request, f"No stopped campaign found for {email_account.email_address} to resume.")

    return redirect("dashboard:index")


@login_required
def campaign_statuses(request):
    user = request.user
    accounts = EmailAccount.objects.filter(user=user)
    data = {}

    # ==========================================
    # 1. PER-ACCOUNT STATS (For the Table Rows)
    # ==========================================
    for account in accounts:
        latest_campaign = (
            CampaignRecord.objects
            .filter(sender_account=account)
            .order_by('-launch_time')
            .only('status', 'sent_count', 'total_recipients')
            .first()
        )

        if latest_campaign:
            data[account.id] = {
                'status': latest_campaign.status or 'N/A',
                'sent_count': latest_campaign.sent_count or 0,
                'total': latest_campaign.total_recipients or 0,
            }
        else:
            data[account.id] = {
                'status': 'N/A',
                'sent_count': 0,
                'total': 0,
            }

    # ==========================================
    # 2. GLOBAL STATS (For the Top Metrics Cards)
    # ==========================================
    
    # --- A. Standard Campaign Totals ---
    std_campaigns = CampaignRecord.objects.filter(sender_account__user=user)
    std_sent = std_campaigns.aggregate(Sum('sent_count'))['sent_count__sum'] or 0
    std_opens = std_campaigns.filter(track_campaign=True).aggregate(Sum('open_rate'))['open_rate__sum'] or 0

    # --- B. Drip Campaign Totals (The "Better Solution") ---
    # We query the 'SentDripEmail' log directly. This is the source of truth and does not reset between steps.
    drip_logs = SentDripEmail.objects.filter(drip_campaign__launched_by=user)
    drip_sent = drip_logs.count()
    drip_opens = drip_logs.filter(is_opened=True).count()

    # --- C. Combined Totals ---
    total_sent_global = std_sent + drip_sent
    total_opens_global = std_opens + drip_opens

    # Calculate Open Rate
    if total_sent_global > 0:
        global_open_rate = (total_opens_global / total_sent_global) * 100
    else:
        global_open_rate = 0.0

    # --- D. Subscription Expiry Logic ---
    expiry_date_str = "N/A"
    days_left_str = ""
    
    if hasattr(user, 'subscription'):
        sub = user.subscription
        if sub.end_date:
            now = timezone.now()
            # Compare timestamps directly
            delta = sub.end_date - now
            
            expiry_date_str = sub.end_date.strftime("%b %d")
            
            if delta.total_seconds() < 0:
                days_left_str = "Expired"
            elif delta.days == 0:
                days_left_str = "Expires today"
            else:
                days_left_str = f"{delta.days} days left"
        else:
            expiry_date_str = "Lifetime"

    # ==========================================
    # 3. FINAL JSON RESPONSE
    # ==========================================
    data['global_stats'] = {
        'total_sent': total_sent_global,
        'total_opens': total_opens_global,
        'open_rate': round(global_open_rate, 1),
        'expiry_date': expiry_date_str,
        'days_left': days_left_str
    }

    return JsonResponse(data)


@login_required
def add_email_account(request):
    form = EmailAccountForm()

    if request.method == "POST":
        form = EmailAccountForm(request.POST)

        if form.is_valid():
            email_account = form.save(commit=False)  # Prevent immediate DB save
            email_account.user = request.user  # Assign user before validation

            try:
                email_account.full_clean()  # Run model-level validation after assigning user
                email_account.save()  # Save only if validation passes

                # send_email_async(email_account, request)  # Send confirmation email
                send_account_attach_notif_email.delay(email_account.id, request.user.id)

                messages.warning(
                    request,
                    "Form Submission Complete!\n\n"
                    "You should receive a confirmation email in a couple of minutes.\n\n"
                    "The email will tell you if the configuration was a success or a failure.\n\n"
                    "For any issues, contact The Dispatch Skool Support."
                )
                return redirect("dashboard:index")

            except ValidationError as e:
                messages.error(request, str(e))  # Show validation error message

        else:
            # Display form validation errors
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.capitalize()}: {error}")

    return render(request, "dashboard/add_email_account.html", {"form": form})


# Update Email Account
@login_required
def email_account_update(request, id):
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    form = EmailAccountForm(instance=email_account)
    
    if request.method == "POST":
        form = EmailAccountForm(request.POST, instance=email_account)
        if form.is_valid():
            form.save()
            email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
            # send_email_async(email_account, request)
            send_account_attach_notif_email.delay(email_account.id, request.user.id)
            messages.warning(
                    request,
                    "Form Submission Complete!\n\n"
                    "You should receive a confirmation email in a couple of minutes.\n\n"
                    "The email will tell you if the configuration was a success  or a failure.\n\n"
                    "For any issues, contact The Dispatch Skool Support."
                )
            return redirect("dashboard:index")
    else:
        form = EmailAccountForm(instance=email_account)
    
    return render(request, "dashboard/add_email_account.html", {"form": form})


@login_required
def email_account_delete(request, id):
    
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    email_account.delete()
    return redirect("dashboard:index")


@login_required
def daily_sheets_view(request):
    """Displays all uploaded daily sheets."""
    sheets = DailySheet.objects.all().order_by('-uploaded_at')[:30]  # Order by latest uploads
    return render(request, 'dashboard/daily_sheets.html', {'sheets': sheets})


@login_required
def coming_soon(request):
    return render(request, 'dashboard/coming_soon.html')



######################################## Views to connect to Gmail API

def oauth_start(request, email_account_id):
    # Store in session for use after OAuth completes
    request.session['connect_email_account_id'] = email_account_id
    scope_param = quote_plus(GOOGLE_SCOPE)

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scope_param}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return redirect(auth_url)


def oauth_callback(request):
    code = request.GET.get('code')
    if not code:
        messages.error(request, "No code provided by Google.")
        return redirect("dashboard:index")

    email_account_id = request.session.pop('connect_email_account_id', None)
    if not email_account_id:
        messages.error(request, "No email account info found. Please try again.")
        return redirect("dashboard:index")

    try:
        email_account = EmailAccount.objects.get(id=email_account_id, user=request.user)
    except EmailAccount.DoesNotExist:
        messages.error(request, "Selected email account does not exist.")
        return redirect("dashboard:index")

    # Exchange code for tokens
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        'code': code,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'grant_type': 'authorization_code',
    }

    response = requests.post(token_url, data=data)
    if response.status_code != 200:
        messages.error(request, f"Token exchange failed: {response.json().get('error_description', 'Unknown error')}")
        return redirect("dashboard:index")

    tokens = response.json()
    access_token = tokens['access_token']

    # Get Gmail profile
    profile_response = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if profile_response.status_code != 200:
        messages.error(request, f"Failed to retrieve Gmail profile: {profile_response.json().get('error', 'Unknown error')}")
        return redirect("dashboard:index")

    profile = profile_response.json()
    gmail_address = profile.get("emailAddress", "").lower()

    # Check if account matches
    if gmail_address != email_account.email_address.lower():
        messages.error(
            request,
            f"Connected Gmail account ({gmail_address}) does not match the selected account ({email_account.email_address})."
        )
        return redirect("dashboard:index")

    # Enforce Gmail domain
    if not gmail_address.endswith("@gmail.com"):
        messages.error(request, "Please connect a valid Gmail account (not a non-Gmail Google account).")
        return redirect("dashboard:index")

    # Save or update GmailToken
    existing_token = GmailToken.objects.filter(email_account=email_account).first()

    refresh_token = tokens.get('refresh_token')
    if not refresh_token and existing_token:
        refresh_token = existing_token.get_refresh_token()

    # Create a temporary instance to set encrypted values
    gmail_token_instance, created = GmailToken.objects.get_or_create(
        email_account=email_account,
        defaults={
            'expires_in': tokens.get('expires_in', 0),
            'token_type': tokens.get('token_type', ''),
            'scope': tokens.get('scope', ''),
            # 'last_history_id': history_id             # Dont create history_id on integration bcz otherwise you wont know if the account is accessed for the first time or has it entered regular checks
        }                                               # as of now, if it doesnt have a history_id, means its 1st time and the inbox scrape will be for last 30 days, if not then only it will ask for any new msg
    )

    # Set encrypted tokens using the new methods
    gmail_token_instance.set_access_token(access_token)
    gmail_token_instance.set_refresh_token(refresh_token)
    gmail_token_instance.save() # Save the instance after setting encrypted fields

    messages.success(request, "Gmail connected successfully!")
    return redirect("dashboard:index")

