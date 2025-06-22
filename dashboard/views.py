from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from users.models import EmailAccount
from leads_data.models import Lead, DailySheet
from .models import GmailToken
from .forms import EmailAccountForm, CampaignForm, BulkCampaignForm
from .tasks import send_emails_chunk_celery_task
from django.db.models import Q, F, Value, IntegerField
from django.db.models.functions import Cast, Replace

from django.contrib import messages
from django.core.mail import get_connection, EmailMessage
from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.cache import cache
from django.utils.timezone import now

from concurrent.futures import ThreadPoolExecutor
from google_secrets import *

import pandas as pd
import requests
import re

######################################## Campaign sending views

# Basic email regex for quick pre-validation (can be more robust if needed)
email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

# def chunk_list(data, chunk_size):
#     """Yield successive chunks of given size from the list."""
#     for i in range(0, len(data), chunk_size):
#         yield data[i:i + chunk_size]


# def send_emails(email_account, leads, subject, body, min_delay, max_delay, chunk_size=150):
    
#     """
#     Sends campaign emails in parallel chunks.
#     Each task handles a small portion of the leads to avoid blocking workers.
#     """
#     total_chunks = 0
#     total_leads = 0
#     for chunk in chunk_list(leads, chunk_size):
#         # Call the Celery task directly using .delay()
        
#         send_emails_chunk_celery_task.delay(
#             email_account.id,
#             chunk, # 👈 Only this chunk of leads
#             subject,
#             body,
#             min_delay,
#             max_delay
#         )
#         print(f"Chunk of {len(chunk)} leads queued to Celery.")
#         total_chunks += 1
#         total_leads += len(chunk)

#     print(f"Total chunks queued: {total_chunks}")
#     print(f"Total leads in all chunks: {total_leads}")


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
        normalized_columns = {col: col.strip().lower().replace(" ", "") for col in df.columns}

        # Find the original column name for email
        email_col = next((col for col, norm in normalized_columns.items() if 'email' in norm), None)

        if not email_col:
            print("Required 'email' column not found in the Excel file.")
            return []

        leads = []
        for _, row in df.iterrows():
            lead = {}

            # Process email: This is the only column strictly necessary for a lead
            email_val = clean_value(row[email_col])
            if not email_val or not re.match(email_regex, email_val):
                # print(f"Skipping row: invalid or missing email: '{email_val}'") # Can uncomment for debugging
                continue
            lead['email'] = email_val

            # Process all other columns without hardcoding their names
            for col in df.columns:
                if col != email_col: # Include all columns except the email column
                    lead[col] = clean_value(row[col])

            leads.append(lead)

        return leads

    except Exception as e:
        print(f"Error in process_excel_file: {e}")
        return []


def get_leads_from_db(starting_mc_number, targets_count,
                      power_units_comparison=None, power_units_value=None,
                      drivers_comparison=None, drivers_value=None,
                      status=None, carrier_operation=None, hm=None, hhg=None,
                      new_entrant=None):
    
    try:
        formatted_mc = f"MC {starting_mc_number}"

        # Base filters
        filters = (
            Q(email__isnull=False) &
            ~Q(email='') &
            ~Q(power_units='') &
            ~Q(drivers='')
        )

        if status and not status == '': # This correctly adds filter only if status is not an empty string
            filters &= Q(status=status)
        if carrier_operation and not carrier_operation == '': # This correctly adds filter only if carrier_operation is not an empty string
            filters &= Q(carrier_operation=carrier_operation)

        if hm == 'Yes':
            filters &= Q(hm='Yes')
        elif hm == 'No':
            filters &= Q(hm='No')
        # If hm is '' (or anything else that's not 'Yes' or 'No'), no filter is added for hm, meaning 'Any' is included.

        if hhg == 'Yes':
            filters &= Q(hhg='Yes')
        elif hhg == 'No':
            filters &= Q(hhg='No')

        if new_entrant == 'Yes':
            filters &= Q(new_entrant='Yes')
        elif new_entrant == 'No':
            filters &= Q(new_entrant='No')

        # Casting string fields to integers before comparison
        queryset = Lead.objects.annotate(
            power_units_int=Cast(Replace(Replace(F('power_units'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
            drivers_int=Cast(Replace(Replace(F('drivers'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
        )

        # Numerical filters will only apply if power_units_value / drivers_value is not None
        if power_units_comparison and power_units_value is not None:
            filters &= Q(power_units_int__isnull=False) # Ensure the cast was successful
            if power_units_comparison == 'gt':
                filters &= Q(power_units_int__gte=power_units_value)
            elif power_units_comparison == 'lt':
                filters &= Q(power_units_int__lte=power_units_value)
            elif power_units_comparison == 'eq':
                filters &= Q(power_units_int=power_units_value)

        if drivers_comparison and drivers_value is not None:
            filters &= Q(drivers_int__isnull=False) # Ensure the cast was successful
            if drivers_comparison == 'gt':
                filters &= Q(drivers_int__gte=drivers_value)
            elif drivers_comparison == 'lt':
                filters &= Q(drivers_int__lte=drivers_value)
            elif drivers_comparison == 'eq':
                filters &= Q(drivers_int=drivers_value)

        # Find closest starting lead
        starting_lead = (
            queryset.filter(mc_number__gte=formatted_mc)
            .filter(filters)
            .order_by('mc_number')
            .first()
        )

        if not starting_lead:
            starting_lead = (
                queryset.filter(mc_number__lte=formatted_mc)
                .filter(filters)
                .order_by('-mc_number')
                .first()
            )

        if not starting_lead:
            return []

        starting_mc = starting_lead.mc_number

        leads_after = list(
            queryset.filter(mc_number__gte=starting_mc)
            .filter(filters)
            .order_by('mc_number')[:targets_count]
        )

        remaining = targets_count - len(leads_after)

        if remaining > 0:
            leads_before = list(
                queryset.filter(mc_number__lt=starting_mc)
                .filter(filters)
                .order_by('-mc_number')[:remaining]
            )
            leads_after.extend(leads_before)

        leads = [
            {
                'mc_number': lead.mc_number,
                'name': lead.legal_name,
                'email': lead.email
            }
            for lead in leads_after[:targets_count]
        ]
        return leads

    except Exception as e:
        print(f"Exception: {e}")
        return []


@login_required
def campaign(request, email_account_id):
    
    email_account = get_object_or_404(EmailAccount, id=email_account_id, user=request.user)
    form = CampaignForm(user=request.user)

    if request.method == 'POST':
        form = CampaignForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            email_subject = form.cleaned_data['email_subject']
            email_body = form.cleaned_data['email_body']
            file_upload = form.cleaned_data['file_upload']
            mc_number = form.cleaned_data['mc_number']
            targets_count = form.cleaned_data['targets_count']
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')

            # Extra filters from the form
            power_units_comparison = form.cleaned_data.get('power_units_comparison')
            power_units_value = form.cleaned_data.get('power_units_value')
            drivers_comparison = form.cleaned_data.get('drivers_comparison')
            drivers_value = form.cleaned_data.get('drivers_value')
            status = form.cleaned_data.get('status')
            carrier_operation = form.cleaned_data.get('carrier_operation')
            hm = form.cleaned_data.get('hm')
            hhg = form.cleaned_data.get('hhg')
            new_entrant = form.cleaned_data.get('new_entrant')

            leads = []
            if file_upload:
                leads = process_excel_file(file_upload)
            elif mc_number and not request.user.on_free_trial:
                leads = get_leads_from_db(
                    mc_number, targets_count,
                    power_units_comparison=power_units_comparison,
                    power_units_value=power_units_value, drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, hm=hm, hhg=hhg, new_entrant=new_entrant
                )

            if not leads:
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:index')
            
            seen_emails = set()
            unique_leads = []
            for lead in leads:
                email = lead.get("email")
                if email and email not in seen_emails:
                    unique_leads.append(lead)
                    seen_emails.add(email)

            leads = unique_leads

            filter_data = {}
            for key in ['mc_number', 'targets_count', 'power_units_comparison', 'power_units_value', 
                        'drivers_comparison', 'drivers_value', 'status', 'carrier_operation', 
                        'hm', 'hhg', 'new_entrant']:
                val = locals().get(key)
                if val not in [None, '', 'None']:
                    filter_data[key] = val

            # Handle AJAX pre-check
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' and not request.POST.get('confirm'):
                res = JsonResponse({
                    'lead_count': len(leads),
                    'filters': filter_data,
                    'confirmed': False
                })
                return res

            print(f"Queuing email campaign to {len(leads)} leads for {email_account.email_address}")
            # send_emails(email_account, leads, email_subject, email_body, min_delay, max_delay)

            # Skip the chunking and directly feed the entire list to the celery worker
            send_emails_chunk_celery_task.delay(email_account.id, request.user.id, leads, email_subject, email_body, min_delay, max_delay)

            email_account.last_used_at = now()
            email_account.save(update_fields=["last_used_at"])

            messages.success(request, f"Success! Emails are being sent for {email_account.email_address}. Thank you for your patience.")
            return redirect('dashboard:index')

    else:
        form = CampaignForm(user=request.user)

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
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    # Unique cache key per user (you can make it tighter using session ID if needed)
    cache_key = f"bulk_leads_{request.user.id}"

    # Load cached data (leads & count)
    cached_data = cache.get(cache_key)
    leads = cached_data['leads'] if cached_data else []

    form = BulkCampaignForm(user=request.user)

    # Step 1: Leads Submission
    if request.method == 'POST' and 'submit_leads' in request.POST:
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            file_upload = form.cleaned_data['file_upload']
            mc_number = form.cleaned_data['mc_number']
            targets_count = form.cleaned_data['targets_count']

            # Extra filters from the form
            power_units_comparison = form.cleaned_data.get('power_units_comparison')
            power_units_value = form.cleaned_data.get('power_units_value')
            drivers_comparison = form.cleaned_data.get('drivers_comparison')
            drivers_value = form.cleaned_data.get('drivers_value')
            status = form.cleaned_data.get('status')
            carrier_operation = form.cleaned_data.get('carrier_operation')
            hm = form.cleaned_data.get('hm')
            hhg = form.cleaned_data.get('hhg')
            new_entrant = form.cleaned_data.get('new_entrant')

            leads = []
            if file_upload:
                leads = process_excel_file(file_upload)
            elif mc_number and not request.user.on_free_trial:
                leads = get_leads_from_db(
                    mc_number, targets_count,
                    power_units_comparison=power_units_comparison,
                    power_units_value=power_units_value, drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, hm=hm, hhg=hhg, new_entrant=new_entrant
                )

            if not leads:
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'error', 'message': 'No valid leads found.'})
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:bulk_campaign')
            
            # Before setting the cache, delete the old one
            cache.delete(cache_key)
            cache.set(cache_key, {'leads': leads, 'leads_available': len(leads)}, timeout=300)

            filter_data = {}
            for key in ['mc_number', 'targets_count', 'power_units_comparison', 'power_units_value', 
                        'drivers_comparison', 'drivers_value', 'status', 'carrier_operation', 
                        'hm', 'hhg', 'new_entrant']:
                val = locals().get(key)
                if val not in [None, '', 'None']:
                    filter_data[key] = val

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'status': 'success',
                    'message': f'{len(leads)} leads found and submitted successfully. Do you wish to proceed?',
                    'leads': leads,  # Include leads data here
                })

            messages.success(request, f"{len(leads)} leads submitted successfully.")
            return redirect('dashboard:bulk_campaign')

        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                errors = {field: error.get_json_data() for field, error in form.errors.items()}
                return JsonResponse({'status': 'error', 'errors': errors})

            messages.error(request, "Invalid form submission.")
            return redirect(request.path)

    # Step 2: Lead Allocation (only available if leads are cached)
    elif request.method == 'POST' and 'submit_allocation' in request.POST:

        cached_data = cache.get(cache_key)
        total_leads = cached_data['leads_available'] if cached_data and 'leads_available' in cached_data else 0
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)
        if not cached_data:
            return redirect('dashboard:bulk_campaign')
        
        if form.is_valid():

            leads = cached_data['leads']
            email_subject = form.cleaned_data.get('email_subject')
            email_body = form.cleaned_data.get('email_body')
            select_all = form.cleaned_data.get('select_all')
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')

            selected_account_ids = request.POST.getlist('selected_accounts')
            account_lead_map = {}
            total_requested_leads = 0

            if select_all:
                # ✅ Get all user email accounts
                accounts = EmailAccount.objects.filter(user=request.user)
                if not accounts.exists():
                    form.add_error(None, "No email accounts found for your user.")
                    return render(request, 'dashboard/bulk_campaign.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })

                # ✅ Auto-distribute leads among accounts
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
                    return render(request, 'dashboard/bulk_campaign.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })

            # ✅ Convert account -> number to account -> list of leads
            if not select_all:
                lead_index = 0
                updated_map = {}

                for account, count in account_lead_map.items():
                    if not isinstance(count, int):
                        try:
                            count = int(count[0]) if isinstance(count, list) else int(count)
                        except (ValueError, TypeError):
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


            def start_campaign():
                # chunk_size = 150 # 100 leads per chunk

                for account, assigned_leads in account_lead_map.items():
                    if assigned_leads:
                        # total_chunks = 0
                        # total_leads = 0

                        print(f"Queuing bulk email campaign to {len(assigned_leads)} leads for {account.email_address}")
                        send_emails_chunk_celery_task.delay(account.id, request.user.id, assigned_leads, email_subject, email_body, min_delay, max_delay)

                        # for chunk in chunk_list(assigned_leads, chunk_size):
                        #     # Call the Celery task directly using .delay()
                        #     task_id = send_emails_chunk_celery_task.delay(
                        #         account.id,
                        #         chunk,          # 👈 chunked leads
                        #         email_subject,
                        #         email_body,
                        #         min_delay,
                        #         max_delay
                        #     )
                        #     print(f"Celery Task Queued: {task_id} for chunk of {len(chunk)} leads.")
                        #     print(f"Queued Celery task {task_id} with chunk of {len(chunk)} leads")
                        #     total_chunks += 1
                        #     total_leads += len(chunk)

                        account.last_used_at = now()
                        account.save(update_fields=["last_used_at"])
                        # print(f"Finished campaign for {account.email_address} — {total_chunks} chunks, ")

                        # print(f"Account {account.email_address}: Total chunks queued to Celery: {total_chunks}")
                        print(f"Account {account.email_address}: Total leads in all chunks: {total_leads}")

            start_campaign()
            cache.delete(cache_key)  # clean up

            messages.success(request, "🎉 Bulk Campaign started successfully! Emails are being sent!")
            return redirect('dashboard:index')

        else:
          # Print the form errors for debugging
          print("Form is invalid.")
          print("Form errors:", form.errors)

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
        'can_launch_bulk_campaign': (request.user.subscription.status == "active" or request.user.on_free_trial)
    })



######################################## Email accounts creation and dashboard views

@login_required
def index(request):
	
  email_accounts = EmailAccount.objects.filter(user=request.user).order_by('-last_used_at')
  for account in email_accounts:
        account.is_gmail = account.email_address.lower().endswith('@gmail.com')
  context = {
		"email_accounts": email_accounts
	}
  return render(request, 'dashboard/index.html', context)


# Email account successfully added confirmation email
def send_email_async(email_account, request):
    """Sends an email in a separate thread using ThreadPoolExecutor."""
    
    def _send_email():
        try:
            decrypted_password = email_account.get_password()

            # Determine the SMTP security type
            use_tls = email_account.server_type == "TLS"
            use_ssl = email_account.server_type == "SSL"

            if use_tls and use_ssl:
                print("Invalid configuration: Cannot enable both TLS and SSL.")
                return

            # Correct credentials entered
            try:
                # Create SMTP connection
                connection = get_connection(
                    backend="django.core.mail.backends.smtp.EmailBackend",
                    host=email_account.host,
                    port=email_account.port_number,
                    username=email_account.email_address,
                    password=decrypted_password,
                    use_tls=use_tls,
                    use_ssl=use_ssl,
                )
                connection.open()

                # Email content
                subject = "Email account configured successfully"
                body = (
                    f"Hello {request.user.first_name},\n\n"
                    f"This is to notify you that your email account {email_account.email_address} "
                    "has been successfully configured with Dispatch Skool and is now ready to launch campaigns.\n\n"
                    "Best Regards,\nThe Dispatch Skool Team."
                )
                from_email = email_account.email_address
                recipient_list = [request.user.email]

                # Create and send email
                email_message = EmailMessage(
                    subject, body, from_email, recipient_list, connection=connection
                )
                email_message.send()
                connection.close()

            # Incorrect credentials entered
            except:
                subject = "Email account configuration failure"
                body = (
                    f"Hello {request.user.first_name},\n\n"
                    f"This is to notify you that your email account {email_account.email_address} "
                    "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                    "In case of any problems, feel free to reach out.\n\n"
                    "Best Regards,\nThe Dispatch Skool Team."
                )
                from_email = settings.EMAIL_HOST_USER
                recipient_list = [request.user.email]

                email_message = EmailMessage(
                    subject,
                    body,
                    from_email,
                    recipient_list,
                )
                email_message.send()

            print(f"Notification email sent to {request.user.email}")

        except Exception as e:
            print(f"Error sending notification email: {e}")

    # Execute in a separate thread
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(_send_email)



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

                send_email_async(email_account, request)  # Send confirmation email

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
            send_email_async(email_account, request)
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

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={GOOGLE_SCOPE}"
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
    GmailToken.objects.update_or_create(
        email_account=email_account,
        defaults={
            'access_token': access_token,
            'refresh_token': tokens.get('refresh_token', ''),
            'expires_in': tokens.get('expires_in', 0),
            'token_type': tokens.get('token_type', ''),
            'scope': tokens.get('scope', ''),
        }
    )

    messages.success(request, "Gmail connected successfully!")
    return redirect("dashboard:index")

