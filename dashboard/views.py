from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods, require_safe, require_GET, require_POST
from django.core.paginator import Paginator

from users.models import EmailAccount, AccountGroup
from leads_data.models import DailySheet
from .models import GmailToken, CampaignRecord, EmailOpen, VerificationBatch, VerificationUsage, CampaignTemplate
from drip_campaigns.models import DripTemplate, EmailAccountAndLeads
from warmup.models import WarmupCampaign
from drip_campaigns.models import SentDripEmail
from .forms import EmailAccountForm, CampaignForm, BulkCampaignForm, TemplateFormSet, VerificationUploadForm
from .tasks import send_emails_chunk_celery_task, send_account_attach_notif_email, verify_email_task
from .utilities import *
from django.db.models import F, Value, OuterRef, Subquery, Prefetch, CharField
from django.db.models.functions import Coalesce

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.utils.timezone import now, make_naive
from django.core.files.base import ContentFile
from django.utils import timezone
from datetime import datetime, timedelta
from django.db import transaction
from django.db.models import Sum

from google_secrets import *
from urllib.parse import quote_plus

import requests
from itertools import chain
import pytz
import dns.resolver
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
    
    template_formset = TemplateFormSet(queryset=CampaignTemplate.objects.none(), prefix='templates')

    if request.method == 'POST':
        post_data = request.POST.copy()
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

        # Bind data to forms
        form = CampaignForm(post_data, files_data, user=request.user)
        template_formset = TemplateFormSet(post_data, prefix='templates')

        if form.is_valid() and template_formset.is_valid():

            # Determine if the campaign as a whole should be marked as "tracking active"
            # We check if at least one template has tracking enabled, and if so, we mark the entire campaign as tracking enabled. 
            # This way, in the email sending logic, we can just check the campaign's track_campaign field to decide whether 
            # to generate tracking pixels and links.
            is_tracking_enabled = any(f.cleaned_data.get('track_template') for f in template_formset.forms if f.cleaned_data)
            
            # --- 1. Extract Main Form Data (Subject/Body removed) ---
            file_upload = form.cleaned_data['file_upload']
            lower_limit_mc_number = form.cleaned_data['lower_limit_mc_number']
            upper_limit_mc_number = form.cleaned_data['upper_limit_mc_number']
            mc_number = form.cleaned_data['mc_number']
            targets_count = form.cleaned_data['targets_count']
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')
            
            scheduled_launch_datetime = form.cleaned_data.get('schedule_launch_datetime')
            skip_mc_numbers = form.cleaned_data.get("skip_mc_numbers")

            # Filters
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

            # --- 2. Lead Fetching Logic ---
            if file_upload:
                leads = process_leads_file(file_upload, request.user)
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
                message = "❌ No valid leads found."
                messages.error(request, message)
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

            # --- 3. Save Templates & Create Campaign (Atomic Transaction) ---
            try:
                with transaction.atomic():
                    # A. Save Templates
                    new_templates = template_formset.save(commit=False)
                    for t in new_templates:
                        t.owner = request.user
                        t.save()
                    
                    # Handle deletions
                    for deleted_obj in template_formset.deleted_objects:
                        deleted_obj.delete()

                    # B. Create Campaign Record (Without subject/body)
                    campaign_data = {
                        'leads_data': leads,
                        'min_delay': min_delay,
                        'max_delay': max_delay,
                        'launched_by': request.user,
                        'sender_account': email_account,
                        'total_recipients': len(leads),
                        'sent_count': 0,
                        'lead_source': 'Excel' if file_upload else 'DB',
                        'track_campaign': is_tracking_enabled
                    }

                    if scheduled_launch_datetime:
                        campaign_data['status'] = 'pending'
                        campaign_data['scheduled_launch_time'] = scheduled_launch_datetime
                        campaign_record = CampaignRecord.objects.create(**campaign_data)
                        
                        # C. Link Templates M2M
                        campaign_record.templates.set(new_templates)
                        
                        pst_tz = pytz.timezone('Asia/Karachi')
                        scheduled_time_pst = scheduled_launch_datetime.astimezone(pst_tz)
                        success_message = f"✅ Campaign scheduled for {scheduled_time_pst.strftime('%Y-%m-%d %H:%M %p %Z')}."
                    
                    else:
                        campaign_data['status'] = 'processing'
                        campaign_record = CampaignRecord.objects.create(**campaign_data)
                        
                        # C. Link Templates M2M
                        campaign_record.templates.set(new_templates)

                        # Queue Task
                        send_emails_chunk_celery_task.delay(campaign_record.id)
                        
                        email_account.last_used_at = timezone.now()
                        email_account.save(update_fields=["last_used_at"])
                        
                        success_message = f"✅ Success! Emails are being sent for {email_account.email_address}."

                messages.success(request, success_message)
                return redirect('dashboard:index')

            except Exception as e:
                print(f"Error creating campaign: {e}")
                messages.error(request, "An error occurred while creating the campaign.")

        # Invalid form
        print("🛑 Form is invalid:", form.errors, template_formset.errors)
        return redirect('dashboard:index')

    return render(request, 'dashboard/campaign.html', {
        'form': form, 
        'template_formset': template_formset,
        'email_account': email_account
    })


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
                leads = process_leads_file(file_upload, request.user)
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
#                 refetched_leads = process_leads_file(f, request.user)

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
    template_formset = TemplateFormSet(queryset=CampaignTemplate.objects.none(), prefix='templates')

    if request.method == 'POST' and 'submit_allocation' in request.POST:
        
        total_leads = cached_data['leads_available'] if cached_data and 'leads_available' in cached_data else 0
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)
        template_formset = TemplateFormSet(request.POST, prefix='templates')

        if not cached_data:
            messages.error(request, "Lead data not found. Please start over.")
            return redirect('dashboard:bulk_campaign')
        
        lead_source = cached_data['lead_source']
        refetched_leads = [] # cause they were fetched once before in the first step

        if lead_source == "Excel":
          file_path = cached_data['file_path']
          with open(file_path, 'rb') as f:
              refetched_leads = process_leads_file(f, request.user)

        elif lead_source == "DB":
            params = cached_data['params']
            refetched_leads = get_leads_from_db(**params)
        
        # If refetch failed or returned no leads, surface a form error and re-render Step 2
        if not refetched_leads:
            form.add_error(None, "No leads could be reloaded. Please restart the bulk campaign flow and try again.")
            return render(request, 'dashboard/bulk_campaign_step2.html', {
                'form': form,
                'email_accounts': email_accounts,
                'template_formset': template_formset,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'leads_count': 0,
            })

        if form.is_valid() and template_formset.is_valid():
            
            is_tracking_enabled = any(f.cleaned_data.get('track_template') for f in template_formset.forms if f.cleaned_data)

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
                        'template_formset': template_formset,
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
                        'template_formset': template_formset,
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
                                'template_formset': template_formset,
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
                    # 1. Save the new Templates from the FormSet
                    new_templates = template_formset.save(commit=False)
                    for template in new_templates:
                        template.owner = request.user
                        template.save()

                    # Handle deletions if any (standard FormSet behavior)
                    for deleted_obj in template_formset.deleted_objects:
                        deleted_obj.delete()

                    # 2. Prepare the CampaignRecord objects
                    # Note: We leave 'subject' and 'body' empty to use the new M2M architecture
                    for account, assigned_leads in account_lead_map.items():
                        if assigned_leads:
                            campaign_params = {
                                'leads_data': assigned_leads,
                                'min_delay': min_delay,
                                'max_delay': max_delay,
                                'launched_by': request.user,
                                'sender_account': account,
                                'total_recipients': len(assigned_leads),
                                'sent_count': 0,
                                'lead_source': lead_source,
                                'track_campaign': is_tracking_enabled
                            }

                            if scheduled_launch_datetime:
                                campaign_params.update({
                                    'status': 'pending',
                                    'scheduled_launch_time': scheduled_launch_datetime
                                })
                                scheduled_campaigns.append(CampaignRecord(**campaign_params))
                            else:
                                campaign_params.update({'status': 'processing'})
                                immediate_campaigns.append(CampaignRecord(**campaign_params))
                                
                                # Track account usage
                                account.last_used_at = timezone.now()
                                accounts_to_update.append(account)

                    # 3. Execution Phase (Postgres returns IDs for bulk_create)
                    created_scheduled = []
                    if scheduled_campaigns:
                        created_scheduled = CampaignRecord.objects.bulk_create(scheduled_campaigns)
                        print(f"Scheduled {len(created_scheduled)} bulk campaigns.")

                    created_immediate = []
                    if immediate_campaigns:
                        created_immediate = CampaignRecord.objects.bulk_create(immediate_campaigns)
                        print(f"Launched {len(created_immediate)} bulk campaigns immediately.")

                    # 4. The "Bridge": Link Many-to-Many Templates
                    # We combine all newly created campaigns to attach the templates
                    all_new_campaigns = created_scheduled + created_immediate
                    
                    # We use the through model's bulk_create for maximum performance 
                    # instead of looping .add() calls
                    CampaignThroughModel = CampaignRecord.templates.through
                    m2m_links = []
                    for campaign in all_new_campaigns:
                        for template in new_templates:
                            m2m_links.append(CampaignThroughModel(
                                campaignrecord_id=campaign.id,
                                campaigntemplate_id=template.id
                            ))
                    
                    if m2m_links:
                        CampaignThroughModel.objects.bulk_create(m2m_links)

                    # 5. Finalize Accounts and Celery Tasks
                    if accounts_to_update:
                        EmailAccount.objects.bulk_update(accounts_to_update, ['last_used_at'])

                    for campaign in created_immediate:
                        send_emails_chunk_celery_task.delay(campaign.id)

                return len(created_scheduled), len(created_immediate)


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
                'template_formset': template_formset,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'leads_count': leads_count,
            })
        

    return render(request, 'dashboard/bulk_campaign_step2.html', {
        'form': form,
        'email_accounts': email_accounts,
        'template_formset': template_formset,
        'email_accounts_count': email_accounts_count,
        'leads_ready': bool(cached_data),
        'leads_count': leads_count,
    })



@require_safe
def track_open(request, unique_identifier):
    ua = request.META.get('HTTP_USER_AGENT', '').lower()
    ip = request.META.get('REMOTE_ADDR', '')

    # We block generic bots, scrapers, and preview tools.
    bot_signatures = [
        'bot', 'spider', 'crawl', 'slurp',       # General crawlers
        'facebookexternalhit', 'whatsapp',       # Social previews
        'curl', 'wget', 'python-requests',       # Scripts
        'headless',                              # Headless browsers
        'preview',                               # Email client preview generators
        'barracuda', 'mimecast',                 # Security scanners
        'trend micro', 'sophos'                  # More security scanners
    ]

    # Quick exit for obvious bots
    if any(sig in ua for sig in bot_signatures):
        print(f"Bot blocked (UA Match): {ua} - IP: {ip}")
        return gif_response()

    try:
        with transaction.atomic():
            email_log = (
                EmailOpen.objects
                .select_for_update()
                .select_related('campaign', 'template')
                .get(unique_identifier=unique_identifier)
            )

            # 2. SUPERHUMAN TIME CHECK
            # Calculate how long it has been since the email was generated.
            now = timezone.now()
            time_diff = (now - email_log.timestamp).total_seconds()

            # If opened in less than 6 seconds, it's likely a security filter pre-fetching the image.
            if time_diff < 6:
                print(f"⚡ Superhuman open ignored ({time_diff:.2f}s): {email_log.recipient_email}")
                return gif_response()

            # 3. RECORD THE OPEN
            # Idempotency check: Only count if not already opened
            if not email_log.is_opened:
                # 1. Update Global Campaign Stats
                campaign = email_log.campaign
                campaign.open_rate = F('open_rate') + 1
                campaign.save(update_fields=['open_rate'])

                # 2. Update Specific Template Stats (A/B Testing)
                if email_log.template:
                    template = email_log.template
                    template.open_rate = F('open_rate') + 1
                    template.save(update_fields=['open_rate'])

                # 3. Mark the log as opened
                email_log.is_opened = True
                email_log.save(update_fields=['is_opened'])

    except EmailOpen.DoesNotExist:
        # Log the error but don't crash
        print(f"Unknown Pixel Hit: {unique_identifier} | IP: {ip} | UA: {ua}")
    
    # Always return the invisible pixel
    return gif_response()


######################################## Email accounts creation and dashboard views


@login_required
def campaign_records(request):
    # 1. Fetch Bulk Templates
    bulk_templates = CampaignTemplate.objects.filter(
        owner=request.user,
        track_template=True
    ).prefetch_related('campaigns').annotate(
        record_type=Value('Bulk', output_field=CharField())
    )
    
    # 2. Fetch Drip Variations
    drip_templates = DripTemplate.objects.filter(
        campaign__launched_by=request.user,
        track_template=True
    ).select_related('campaign').annotate(
        record_type=Value('Drip', output_field=CharField(max_length=10))
    )

    # 3. Combine and Sort by most recent
    combined_list = sorted(
        chain(bulk_templates, drip_templates),
        key=lambda instance: (
            instance.created_at if hasattr(instance, 'created_at') and instance.created_at 
            else getattr(instance.campaign, 'created_at', timezone.now()) if hasattr(instance, 'campaign')
            else timezone.now()
        ),
        reverse=True
    )

    paginator = Paginator(combined_list, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'dashboard/campaign_records.html', {'page_obj': page_obj})

class Echo:
    """An object that implements just the write method of the file-like interface."""
    def write(self, value):
        return value

# @login_required
# def export_email_opens(request):
    
#     queryset = EmailOpen.objects.filter(
#         launched_by=request.user, is_opened=True
#     ).values_list('mc_number', 'legal_name', 'recipient_email').iterator()

#     # 2. Determine Date for Filename "Email_Opens_till_{date}"
#     latest_open = EmailOpen.objects.filter(launched_by=request.user).select_related('campaign').order_by('-timestamp').first()
#     date_str = now().date().isoformat()
    
#     if latest_open:
#         if latest_open.campaign:
#             # Case A: Campaign still exists -> Use launch time
#             target_time = latest_open.campaign.launch_time or latest_open.campaign.scheduled_launch_time
#             if target_time:
#                 date_str = target_time.date().isoformat()
#         else:
#             # Case B: Campaign was deleted by Celery -> Use the open timestamp
#             if latest_open.timestamp:
#                 date_str = latest_open.timestamp.date().isoformat()

#     # 3. Define the Generator
#     def stream_csv():
#         buffer = Echo()
#         writer = csv.writer(buffer)
        
#         yield writer.writerow(["MC Number", "Legal Name", "Email"])

#         for row in queryset:
#             yield writer.writerow(row)

#     # 4. Construct Streaming Response
#     response = StreamingHttpResponse(stream_csv(), content_type="text/csv")
#     response['Content-Disposition'] = f'attachment; filename="Email_Opens__till_{date_str}.csv"'
    
#     return response

@login_required
def export_email_opens(request):
    
    # 1. Fetch Legacy Opens
    legacy_opens = EmailOpen.objects.filter(
        launched_by=request.user, 
        is_opened=True
    ).annotate(
        source=Value('Standard')
    ).values_list('mc_number', 'legal_name', 'recipient_email', 'source')

    # 2. Build a Map for Drip Campaign Leads (Email -> Name)
    # We fetch all lead data for this user's active/completed drip campaigns
    drip_name_map = {}
    lead_blobs = EmailAccountAndLeads.objects.filter(
        campaign__launched_by=request.user
    ).values_list('leads_data', flat=True)

    for blob in lead_blobs:
        if not blob: continue
        for lead in blob:
            email = lead.get('Email') or lead.get('email')
            if not email: continue
            
            # Find a name key (Legal Name, Name, full_name, etc.)
            name_key = next((k for k in lead.keys() if 'name' in k.lower()), None)
            if name_key:
                drip_name_map[email] = lead[name_key]

    # 3. Fetch Drip Opens
    drip_opens_qs = SentDripEmail.objects.filter(
        drip_campaign__launched_by=request.user,
        is_opened=True
    ).values_list('lead_mc_number', 'lead_email')

    # 4. Filename Date Logic (Optimized)
    last_legacy = EmailOpen.objects.filter(launched_by=request.user).order_by('-timestamp').only('timestamp').first()
    last_drip = SentDripEmail.objects.filter(drip_campaign__launched_by=request.user).order_by('-created_at').only('created_at').first()
    
    dates = [d for d in [getattr(last_legacy, 'timestamp', None), getattr(last_drip, 'created_at', None)] if d]
    date_str = max(dates).date().isoformat() if dates else now().date().isoformat()

    # 5. Generator with Dynamic Lookup
    def stream_csv():
        buffer = Echo()
        writer = csv.writer(buffer)
        yield writer.writerow(["MC Number", "Legal Name", "Email", "Campaign Type"])

        # Stream Legacy
        for row in legacy_opens.iterator():
            yield writer.writerow(row)

        # Stream Drip (Enriched with the name map)
        for mc, email in drip_opens_qs.iterator():
            name = drip_name_map.get(email, "N/A")
            yield writer.writerow([mc, name, email, "Drip"])

    return StreamingHttpResponse(stream_csv(), content_type="text/csv", 
                                headers={'Content-Disposition': f'attachment; filename="Email_Opens__till_{date_str}.csv"'})


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
        request.user.on_free_trial or (
            user_subscription is not None and
            user_subscription.status == "active" and
            user_subscription.type in ("warmup", "premium")
        )
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

    context = {
        "form": form,
        "email_account": None,
        "is_verified": False,
        "tracking_domain": None
    }
    return render(request, "dashboard/add_email_account.html", context)


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
    
    context = {
        "form": form, 
        "email_account": email_account, 
        "is_verified": email_account.tracking_domain_verified,
        "tracking_domain": email_account.tracking_custom_domain 
    }
    return render(request, "dashboard/add_email_account.html", context)


@login_required
@require_POST
def verify_account_dns(request, account_id):
    """
    Verifies the DNS for a specific EmailAccount.
    Triggered via AJAX from the 'Edit Email Account' modal/page.
    """
    # 1. Secure Lookup (Ensure user owns this account)
    account = get_object_or_404(EmailAccount, id=account_id, user=request.user)
    domain = account.tracking_custom_domain

    if not domain:
        return JsonResponse({'success': False, 'error': 'No domain saved for this account.'})

    REQUIRED_TARGET = "whitelabel.dispatchskool.com."
    
    try:
        answers = dns.resolver.resolve(domain, 'CNAME')
        for rdata in answers:
            target = rdata.target.to_text()
            if target.rstrip('.') == REQUIRED_TARGET.rstrip('.'):
                
                # SUCCESS
                account.tracking_domain_verified = True
                account.save(update_fields=['tracking_domain_verified'])
                return JsonResponse({'success': True, 'message': 'Account domain verified!'})

        return JsonResponse({
            'success': False, 
            'error': f'CNAME points to {target}, not {REQUIRED_TARGET}'
        })

    except dns.resolver.NoAnswer:
        return JsonResponse({'success': False, 'error': 'No CNAME record found.'})
    except dns.resolver.NXDOMAIN:
        return JsonResponse({'success': False, 'error': 'Domain does not exist.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


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


@login_required
def scraper_donwload(request):
    return render(request, 'dashboard/scraper_dnld.html')


@login_required
def verification_dashboard(request):
    # 1. Get or create the user's daily usage wallet
    usage, created = VerificationUsage.objects.get_or_create(user=request.user)
    
    # 2. Reset daily usage if cooldown is over
    usage.check_and_reset()
    
    # 3. Get user's limit and check if unlimited (superuser)
    limit = usage.get_limit()
    is_unlimited = limit == float('inf')

    # Remaining quota (infinity for superusers)
    remaining = limit - usage.used_count if not is_unlimited else float('inf')

    if request.method == 'POST':
        form = VerificationUploadForm(request.POST, request.FILES)
        
        # Early exit: daily quota reached (skip for superusers)
        if not is_unlimited and usage.used_count >= limit:
            reset_time = usage.next_reset_at.strftime('%H:%M') if usage.next_reset_at else "unknown"
            messages.error(request, f"Daily limit reached. Resets at {reset_time}.")
            return redirect('dashboard:verification_dashboard')

        if form.is_valid():
            uploaded_file = request.FILES['file']

            # 4. Process file in memory & deduplicate emails
            raw_leads_list = process_leads_file(uploaded_file, request.user)
            
            clean_leads_list = []
            seen_emails = set()
            for row in raw_leads_list:
                email_val = str(row.get('Email', '')).strip().lower()
                if email_val and email_val not in seen_emails:
                    seen_emails.add(email_val)
                    clean_leads_list.append(row)

            file_row_count = len(clean_leads_list)

            if file_row_count == 0:
                messages.error(request, "No valid (unique) emails found in this file.")
                return redirect('dashboard:verification_dashboard')

            # --- Per-upload limit for all users ---
            per_upload_limit = 10000
            if file_row_count > per_upload_limit:
                messages.error(
                    request,
                    f"File too large. Maximum {per_upload_limit} unique emails are allowed per upload. "
                    f"This file has {file_row_count}."
                )
                return redirect('dashboard:verification_dashboard')

            # 5. Check daily quota for regular users
            if not is_unlimited and (usage.used_count + file_row_count) > limit:
                messages.error(
                    request, 
                    f"File too large. You have {remaining} credits left, but this file has {file_row_count} unique emails."
                )
                return redirect('dashboard:verification_dashboard')

            # 6. Deduct credits & start 24h timer for normal users
            if not request.user.is_superuser:
                if usage.used_count == 0:
                    usage.next_reset_at = timezone.now() + timedelta(hours=24)
                usage.used_count += file_row_count
                usage.save()

            # 7. Save batch & trigger Celery task
            batch = VerificationBatch(
                user=request.user,
                original_filename=uploaded_file.name,
                status='PROCESSING'
            )

            json_content = json.dumps(clean_leads_list)
            file_name = f"staging_{request.user.id}_{uploaded_file.name.split('.')[0]}.json"
            batch.clean_data_file.save(file_name, ContentFile(json_content))
            batch.save()
            
            verify_email_task.delay(batch.id)

            used_message = (
                f"Used {file_row_count} credits (Duplicates removed)." 
                if not is_unlimited else f"Processing {file_row_count} emails (Duplicates removed)."
            )
            messages.success(request, f"Processing started. {used_message}")
            return redirect('dashboard:verification_dashboard')
    else:
        form = VerificationUploadForm()

    # Fetch user's previous batches
    batches = VerificationBatch.objects.filter(user=request.user)

    context = {
        'form': form,
        'batches': batches,
        'usage': usage,
        'limit': limit,
        'is_unlimited': is_unlimited,
        'remaining': remaining,
        'is_locked': False if is_unlimited else usage.used_count >= limit,
    }

    return render(request, 'dashboard/verification_dashboard.html', context)


@require_GET
@login_required
def batch_status_api(request):
    batch_ids = request.GET.getlist('ids[]')
    
    if not batch_ids:
        return JsonResponse({}, status=200)

    batches = VerificationBatch.objects.filter(
        id__in=batch_ids, 
        user=request.user
    )

    results = {}
    for batch in batches:
        # Securely get the URL only if the file actually exists on storage
        download_url = None
        try:
            if batch.output_file and hasattr(batch.output_file, 'url'):
                download_url = batch.output_file.url
        except ValueError:
            download_url = None

        results[batch.id] = {
            'status': batch.status,
            'is_downloadable': batch.is_downloadable,
            'download_url': download_url,
        }
    
    return JsonResponse(results)

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

