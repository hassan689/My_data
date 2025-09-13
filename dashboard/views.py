from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods, require_safe
from django.core.paginator import Paginator

from users.models import EmailAccount
from leads_data.models import Lead, DailySheet
from .models import GmailToken, CampaignRecord, EmailOpen
from warmup.models import WarmupCampaign
from .forms import EmailAccountForm, CampaignForm, BulkCampaignForm
from .tasks import send_emails_chunk_celery_task, send_account_attach_notif_email
from django.db.models import Q, F, Value, IntegerField, OuterRef, Subquery, Prefetch
from django.db.models.functions import Cast, Replace, Coalesce

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.utils.timezone import now
from datetime import datetime
from django.core.mail import send_mail
from django.utils.timezone import make_naive
from django.db import transaction

from google_secrets import *
from urllib.parse import quote_plus

import pandas as pd
import requests
import re
import pytz
import json

######################################## Campaign sending views

# Basic email regex for quick pre-validation (can be more robust if needed)
email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


def process_excel_file(file):
    
    if not file.name.endswith('.xlsx'):
        return []

    email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

    try:
        df = pd.read_excel(file)

        def clean_value(val):
            if pd.isnull(val):
                return ''
            if isinstance(val, float) and val.is_integer():
                return str(int(val))
            return str(val).strip()

        # Normalize column names for internal lookup (finding 'email' column)
        normalized_columns_map = {col: col.strip().lower().replace(" ", "") for col in df.columns}

        # Find the original column name for email using common variations
        # This will find the first column whose normalized name contains 'email'
        email_col = None
        for col, norm_col in normalized_columns_map.items():
            # Search for 'email' (lowercase) in the normalized column name
            if 'email' in norm_col: 
                email_col = col
                break 

        if not email_col:
            print("Required 'Email' column not found in the Excel file.")
            return []

        leads = []
        for _, row in df.iterrows():
            lead = {}

            # Process email: This is the only column strictly necessary for a lead
            email_val = clean_value(row[email_col])
            if not email_val or not re.match(email_regex, email_val):
                continue
            lead['Email'] = email_val

            # Process all other columns without hardcoding their names
            for col in df.columns:
                if col != email_col: 
                    lead[col] = clean_value(row[col])

            leads.append(lead)

        return leads

    except Exception as e:
        print(f"Error in process_excel_file: {e}")
        return []



def get_leads_from_db(starting_mc_number=None, targets_count=None,
                      lower_limit_mc_number=None, upper_limit_mc_number=None,  # <-- Added support for range
                      power_units_comparison=None, power_units_value=None,
                      drivers_comparison=None, drivers_value=None,
                      status=None, carrier_operation=None, skip_mc_numbers=None,
                      cargo_classification_search_term=None, cargo_info_search_term=None):
    try:
        queryset = Lead.objects.all()

        if skip_mc_numbers:
            # Check if the input is a JSON string and parse it
            if isinstance(skip_mc_numbers, str) and skip_mc_numbers.startswith('['):
                skip_mc_numbers = json.loads(skip_mc_numbers)
            
            # Now, format the list to get just the values
            skip_mc_numbers_formatted = [item['value'] for item in skip_mc_numbers]
            skip_mc_numbers_formatted = [f"MC {mc}" if not str(mc).startswith("MC") else mc for mc in skip_mc_numbers_formatted]
            queryset = queryset.exclude(mc_number__in=skip_mc_numbers_formatted)

        # Apply numerical cleaning
        queryset = queryset.filter(
            power_units__regex=r'^\s*\d+\s*$',
            drivers__regex=r'^\s*\d+\s*$',
        )

        queryset = queryset.annotate(
            power_units_int=Cast(Replace(Replace(F('power_units'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
            drivers_int=Cast(Replace(Replace(F('drivers'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
        )

        filters = (
            Q(email__isnull=False) &
            ~Q(email='') &
            ~Q(power_units='') &
            ~Q(drivers='')
        )

        if status and not status == '':
            filters &= Q(status=status)
        if carrier_operation and not carrier_operation == '':
            filters &= Q(carrier_operation=carrier_operation)

        # --- START OF NEW TAGIFY LOGIC FOR CARGO CLASSIFICATION ---
        cargo_classification_list = []
        if cargo_classification_search_term:
            # Parse the JSON from Tagify if it's a string
            if isinstance(cargo_classification_search_term, str) and cargo_classification_search_term.startswith('['):
                try:
                    parsed_list = json.loads(cargo_classification_search_term)
                    cargo_classification_list = [item['value'] for item in parsed_list]
                except json.JSONDecodeError:
                    # Fallback to single string if parsing fails
                    cargo_classification_list = [cargo_classification_search_term]
            elif isinstance(cargo_classification_search_term, list):
                # If it's already a list (from a previous form cleaning step)
                cargo_classification_list = cargo_classification_search_term
            else:
                # Treat as a single string
                cargo_classification_list = [cargo_classification_search_term]

        if cargo_classification_list:
            cargo_classification_filters = Q()
            for term in cargo_classification_list:
                term_lower = term.lower()
                cargo_classification_filters |= Q(
                    cargo_classifications__icontains=term_lower
                )
            queryset = queryset.filter(
                Q(cargo_classifications__isnull=False) & ~Q(cargo_classifications='')
            ).filter(cargo_classification_filters)
        # --- END OF NEW TAGIFY LOGIC ---

        # --- START OF NEW TAGIFY LOGIC FOR CARGO INFO ---
        cargo_info_list = []
        if cargo_info_search_term:
            # Parse the JSON from Tagify if it's a string
            if isinstance(cargo_info_search_term, str) and cargo_info_search_term.startswith('['):
                try:
                    parsed_list = json.loads(cargo_info_search_term)
                    cargo_info_list = [item['value'] for item in parsed_list]
                except json.JSONDecodeError:
                    # Fallback to single string if parsing fails
                    cargo_info_list = [cargo_info_search_term]
            elif isinstance(cargo_info_search_term, list):
                # If it's already a list (from a previous form cleaning step)
                cargo_info_list = cargo_info_search_term
            else:
                # Treat as a single string
                cargo_info_list = [cargo_info_search_term]

        if cargo_info_list:
            cargo_info_filters = Q()
            for term in cargo_info_list:
                term_lower = term.lower()
                cargo_info_filters |= Q(
                    cargo_info__icontains=term_lower
                )
            queryset = queryset.filter(
                Q(cargo_info__isnull=False) & ~Q(cargo_info='')
            ).filter(cargo_info_filters)
        # --- END OF NEW TAGIFY LOGIC ---

        if power_units_comparison and power_units_value is not None:
            filters &= Q(power_units_int__isnull=False)
            if power_units_comparison == 'gt':
                filters &= Q(power_units_int__gte=power_units_value)
            elif power_units_comparison == 'lt':
                filters &= Q(power_units_int__lte=power_units_value)
            elif power_units_comparison == 'eq':
                filters &= Q(power_units_int=power_units_value)

        if drivers_comparison and drivers_value is not None:
            filters &= Q(drivers_int__isnull=False)
            if drivers_comparison == 'gt':
                filters &= Q(drivers_int__gte=drivers_value)
            elif drivers_comparison == 'lt':
                filters &= Q(drivers_int__lte=drivers_value)
            elif drivers_comparison == 'eq':
                filters &= Q(drivers_int=drivers_value)

        queryset = queryset.filter(filters)

        leads = []  # <-- Unified output container

        # ---------- START OF NEW RANGE MODE SUPPORT ----------
        if lower_limit_mc_number and upper_limit_mc_number and not starting_mc_number:
            lower_mc = f"MC {lower_limit_mc_number}"
            upper_mc = f"MC {upper_limit_mc_number}"

            lower_bound = (
                queryset.filter(mc_number__gte=lower_mc)
                .order_by('mc_number')
                .first()
            )

            upper_bound = (
                queryset.filter(mc_number__lte=upper_mc)
                .order_by('-mc_number')
                .first()
            )

            if not lower_bound or not upper_bound:
                return []

            leads = list(
                queryset.filter(mc_number__gte=lower_bound.mc_number,
                                mc_number__lte=upper_bound.mc_number)
                .order_by('mc_number')
            )
        # ---------- END OF RANGE MODE ----------

        # ---------- DEFAULT STARTING MC + TARGET COUNT MODE ----------
        elif starting_mc_number and targets_count:
            formatted_mc = f"MC {starting_mc_number}"

            starting_lead = (
                queryset.filter(mc_number__gte=formatted_mc)
                .order_by('mc_number')
                .first()
            )

            if not starting_lead:
                starting_lead = (
                    queryset.filter(mc_number__lte=formatted_mc)
                    .order_by('-mc_number')
                    .first()
                )

            if not starting_lead:
                return []

            starting_mc = starting_lead.mc_number

            leads_after = list(
                queryset.filter(mc_number__gte=starting_mc)
                .order_by('mc_number')[:targets_count]
            )

            remaining = targets_count - len(leads_after)

            if remaining > 0:
                leads_before = list(
                    queryset.filter(mc_number__lt=starting_mc)
                    .order_by('-mc_number')[:remaining]
                )
                leads_after.extend(leads_before)

            leads = leads_after[:targets_count]
        # ---------- END OF DEFAULT MODE ----------

        else:
            return []

        # ---------- UNIFIED FINAL OUTPUT FORMATTING ----------
        formatted = [
            {
                'MC Number': lead.mc_number,
                'Legal Name': lead.legal_name,
                'Email': lead.email,
                'U.S DOT': lead.usdot,
                'VMT Year': lead.vmt_year,
                'Power Units': lead.power_units,
                'DUNS Number': lead.duns_number,
                'Drivers': lead.drivers,
                'Cargo Classifications': lead.cargo_classifications,
                'Cargo Info': lead.cargo_info,
                'Telephone': lead.telephone,
                'Address': lead.address,
            }
            for lead in leads
        ]

        return formatted

    except Exception as e:
        print(f"Exception: {e}")
        return []



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
                leads = process_excel_file(file_upload)
                lead_source = 'Excel'
                debug_info['lead_source'] = 'Excel'
                debug_info['leads_count'] = len(leads)

            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                leads = get_leads_from_db(
                    mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
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
                send_emails_chunk_celery_task.delay(email_account.id, leads, email_subject, email_body, min_delay, max_delay, new_camp.id)
                email_account.last_used_at = now()
                email_account.save(update_fields=["last_used_at"])

                success_message = f"✅ Success! Campaign creation complete!\nPress OK to proceed."

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
        if is_ajax or request.POST.get("confirm"):
            return JsonResponse({
                'success': False,
                'message': "Form is invalid.",
                'errors': form.errors.get_json_data()
            }, status=400)

        messages.error(request, "Form submission failed due to validation errors.")
        return redirect('dashboard:index')

    return render(request, 'dashboard/campaign.html', {'form': form, 'email_account': email_account})


def distribute_leads_among_accounts(leads, accounts):
    total_leads = len(leads)
    total_accounts = len(accounts)
    base_count = total_leads // total_accounts
    remainder = total_leads % total_accounts

    lead_index = 0
    account_lead_map = {}

    for i, account in enumerate(accounts):
        count = base_count + (1 if i < remainder else 0)
        assigned_leads = leads[lead_index:lead_index + count]
        lead_index += count
        account_lead_map[account] = assigned_leads

    return account_lead_map


@login_required
@require_http_methods(["GET", "POST"])
def bulk_campaign(request):
    """
    Handles bulk campaign lead submission and allocation.
    Correctly responds with JSON for AJAX requests and redirects/renders for
    standard form submissions.
    """
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    cache_key = f"bulk_leads_{request.user.id}"

    # Load cached data (leads & count)
    cached_data = cache.get(cache_key)
    leads = cached_data['leads'] if cached_data and 'leads' in cached_data else []

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    # Step 1: Leads Submission
    if request.method == 'POST' and 'submit_leads' in request.POST:
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            file_upload = form.cleaned_data.get('file_upload')
            mc_number = form.cleaned_data.get('mc_number')
            lower_limit_mc_number = form.cleaned_data.get('lower_limit_mc_number')
            upper_limit_mc_number = form.cleaned_data.get('upper_limit_mc_number')
            targets_count = form.cleaned_data.get('targets_count')
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
            lead_source = 'N/A' # Default source
            
            if file_upload:
                lead_source = 'Excel'
                leads = process_excel_file(file_upload)
            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                lead_source = 'DB'
                leads = get_leads_from_db(
                    mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
                    power_units_comparison=power_units_comparison, power_units_value=power_units_value, 
                    drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, skip_mc_numbers=skip_mc_numbers,
                    cargo_classification_search_term=cargo_classification_search, cargo_info_search_term=cargo_info_search
                )

            if not leads:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': 'No valid leads found.'})
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:bulk_campaign')
            
            # Before setting the cache, delete the old one
            cache.delete(cache_key)
            cache.set(cache_key, {'leads': leads, 'leads_available': len(leads), 'lead_source': lead_source}, timeout=300)

            # Note: The filter_data dictionary is not used, but kept for context
            filter_data = {}
            for key in ['mc_number', 'targets_count', 'power_units_comparison', 'power_units_value', 
                        'drivers_comparison', 'drivers_value', 'status', 'carrier_operation',
                        'cargo_classification_search', 'cargo_info_search']:
                val = locals().get(key)
                if val not in [None, '', 'None']:
                    filter_data[key] = val

            if is_ajax:
                return JsonResponse({
                    'status': 'success',
                    'message': f'{len(leads)} leads found and submitted successfully. Do you wish to proceed?',
                    'leads': leads,
                })

            messages.success(request, f"{len(leads)} leads submitted successfully.")
            return redirect('dashboard:bulk_campaign')

        else: # form is not valid for 'submit_leads'
            if is_ajax:
                errors = {field: error.get_json_data() for field, error in form.errors.items()}
                return JsonResponse({'status': 'error', 'errors': errors})
            
            messages.error(request, "Invalid form submission.")
            return redirect(request.path)

    # Step 2: Lead Allocation (only available if leads are cached)
    elif request.method == 'POST' and 'submit_allocation' in request.POST:
        cached_data = cache.get(cache_key)
        total_leads = cached_data.get('leads_available', 0)
        
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)

        if not cached_data:
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': 'Cached leads not found. Please resubmit leads.'})
            messages.error(request, "Cached leads not found. Please resubmit leads.")
            return redirect('dashboard:bulk_campaign')
        
        leads = cached_data['leads']
        
        if form.is_valid():
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
                accounts = EmailAccount.objects.filter(user=request.user)
                if not accounts.exists():
                    if is_ajax:
                        return JsonResponse({'status': 'error', 'message': 'No email accounts found for your user.'})
                    form.add_error(None, "No email accounts found for your user.")
                    return render(request, 'dashboard/bulk_campaign.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })
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
                    if is_ajax:
                        return JsonResponse({'status': 'error', 'message': f"Total assigned leads ({total_requested_leads}) must match total available ({len(leads)})."})
                    form.add_error(None, f"Total assigned leads ({total_requested_leads}) must match total available ({len(leads)}).")
                    return render(request, 'dashboard/bulk_campaign.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })

                # Convert account -> number to account -> list of leads
                lead_index = 0
                updated_map = {}
                for account, count in account_lead_map.items():
                    if not isinstance(count, int):
                        try:
                            count = int(count[0]) if isinstance(count, list) else int(count)
                        except (ValueError, TypeError):
                            if is_ajax:
                                return JsonResponse({'status': 'error', 'message': f"Invalid lead count for account {account}"})
                            form.add_error(None, f"Invalid lead count for account {account}")
                            return render(request, 'dashboard/bulk_campaign.html', {
                                'form': form,
                                'email_accounts': email_accounts,
                                'email_accounts_count': email_accounts_count,
                                'leads_ready': bool(cached_data),
                                'total_leads': len(leads),
                            })
                    updated_map[account] = leads[lead_index:lead_index + count]
                    lead_index += count
                account_lead_map = updated_map

            def start_campaign_processing():
                # ... (campaign creation logic is the same)
                scheduled_campaign_count = 0
                immediate_campaign_count = 0
                for account, assigned_leads in account_lead_map.items():
                    if assigned_leads:
                        if scheduled_launch_datetime:
                            # Save as a scheduled record for each account
                            CampaignRecord.objects.create(
                                subject=email_subject,
                                body=email_body,
                                leads_data=assigned_leads,
                                min_delay=min_delay,
                                max_delay=max_delay,
                                scheduled_launch_time=scheduled_launch_datetime,
                                launched_by=request.user,
                                sender_account=account,
                                total_recipients=len(assigned_leads),
                                sent_count=0,
                                status='pending',
                                lead_source=lead_source,
                                track_campaign=track_campaign
                            )
                            scheduled_campaign_count += 1
                            print(f"Scheduled bulk campaign for {account.email_address} with {len(assigned_leads)} leads.")
                        else:
                            # Immediate send
                            print(f"Queuing immediate bulk email campaign to {len(assigned_leads)} leads for {account.email_address}")
                            new_camp = CampaignRecord.objects.create(
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
                            )
                            send_emails_chunk_celery_task.delay(account.id, assigned_leads, email_subject, email_body, min_delay, max_delay, new_camp.id)
                            immediate_campaign_count += 1
                            account.last_used_at = now()
                            account.save(update_fields=["last_used_at"])
                return scheduled_campaign_count, immediate_campaign_count

            scheduled_count, immediate_count = start_campaign_processing()
            cache.delete(cache_key) # Clean up cache

            # This block handles both AJAX and non-AJAX success messages
            if is_ajax:
                return JsonResponse({'status': 'success', 'message': '✅ Success! Campaign creation complete!\nPress OK to proceed.'})

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

            return redirect('dashboard:index')

        else: # form is not valid for 'submit_allocation'
            if is_ajax:
                errors = form.errors.get_json_data()
                return JsonResponse({'status': 'error', 'errors': errors})
            messages.error(request, "Failed to launch campaign due to validation errors.")
            return render(request, 'dashboard/bulk_campaign.html', {
                'form': form,
                'email_accounts': email_accounts,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'total_leads': len(leads),
            })

    # GET Request or Initial Page Load
    if request.method == 'GET':
        form = BulkCampaignForm(user=request.user)

    cached_data = cache.get(cache_key)
    leads_available = len(cached_data['leads']) if cached_data else 0

    return render(request, 'dashboard/bulk_campaign.html', {
        'form': form,
        'email_accounts': email_accounts,
        'email_accounts_count': email_accounts_count,
        'leads_ready': bool(cached_data),
        'total_leads': leads_available,
        # 'can_launch_bulk_campaign': (request.user.subscription.status == "active" or request.user.on_free_trial)
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
                return _gif_response()  # Exit without increment

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

    return _gif_response()



def _gif_response():
    response = HttpResponse(
        b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00,\x00\x00\x00\x00'
        b'\x01\x00\x01\x00\x00\x02\x02D\x01\x00;',
        content_type='image/gif'
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response



######################################## Email accounts creation and dashboard views


@login_required
def campaign_records(request):
    
    # Filter campaigns for the current user with a 'launched' status
    campaign_list = CampaignRecord.objects.filter(
        launched_by=request.user,
        status='launched',
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

    context = {
        "email_accounts": email_accounts,
        "is_warmup_eligible": is_warmup_eligible,
        "is_unibox_eligible": is_unibox_eligible
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
        latest_processing_campaign.save(update_fields=['status'])

        messages.success(
            request,
            f"Campaign '{latest_processing_campaign.subject}' has been cancelled."
        )

    else:
        messages.info(request, f"No active campaign found for {email_account.email_address} to stop.")

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
            latest_cancelled_campaign.save(update_fields=['status'])
            
            send_emails_chunk_celery_task.apply_async(
                (
                    email_account.id, 
                    latest_cancelled_campaign.leads_data, 
                    latest_cancelled_campaign.subject, 
                    latest_cancelled_campaign.body, 
                    latest_cancelled_campaign.min_delay, 
                    latest_cancelled_campaign.max_delay, 
                    latest_cancelled_campaign.id
                ),
                eta=latest_cancelled_campaign.scheduled_launch_time
            )

            messages.success(
                request,
                f"Campaign '{latest_cancelled_campaign.subject}' has been rescheduled to its original launch time."
            )
        else:
            # Revert status to processing and launch immediately
            latest_cancelled_campaign.status = 'processing'
            latest_cancelled_campaign.save(update_fields=['status'])

            # Recall the celery worker for that stopped campaign
            send_emails_chunk_celery_task.delay(
                email_account.id, 
                latest_cancelled_campaign.leads_data, 
                latest_cancelled_campaign.subject, 
                latest_cancelled_campaign.body, 
                latest_cancelled_campaign.min_delay, 
                latest_cancelled_campaign.max_delay, 
                latest_cancelled_campaign.id
            )

            messages.success(
                request,
                f"Campaign '{latest_cancelled_campaign.subject}' has been resumed successfully."
            )
    else:
        messages.info(request, f"No stopped campaign found for {email_account.email_address} to resume.")

    return redirect("dashboard:index")


@login_required
def campaign_statuses(request):
    accounts = EmailAccount.objects.filter(user=request.user)
    data = {}

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

