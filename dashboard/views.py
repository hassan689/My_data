from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from users.models import EmailAccount
from .forms import EmailAccountForm, CampaignForm, BulkCampaignForm
from leads_data.models import Lead, DailySheet
import pandas as pd
from django.utils.timezone import now
from django.contrib import messages
from django.core.mail import get_connection, EmailMultiAlternatives, EmailMessage
from concurrent.futures import ThreadPoolExecutor
from django.core.exceptions import ValidationError
import time
from django.conf import settings
from threading import Thread
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from email.utils import make_msgid
from unibox.models import EmailThread
from django_mailbox.models import Mailbox
from .models import OutgoingEmailMessage

######################################## Campaign sending views


def send_emails(email_account, leads, subject, body, delay, mailbox):
    """Sends multiple personalized emails using Django's `EmailMultiAlternatives` in a separate thread."""

    def _send():
        try:
            # Decrypt the stored password before using it
            decrypted_password = email_account.get_password()

            # Determine the SMTP security type
            use_tls = email_account.server_type == "TLS"
            use_ssl = email_account.server_type == "SSL"

            if use_tls and use_ssl:
                print("Invalid configuration: Cannot enable both TLS and SSL.")
                return

            # Create a custom SMTP connection using Django's EmailBackend
            connection = get_connection(
                backend="django.core.mail.backends.smtp.EmailBackend",
                host=email_account.host,
                port=email_account.port_number,
                username=email_account.email_address,
                password=decrypted_password,
                use_tls=use_tls,
                use_ssl=use_ssl,
            )

            # Open the SMTP connection
            try:
              connection.open()
            except Exception as e:
              print(f"exception while opening connection: {e}")

            # Send personalized emails
            for lead in leads:
                personalized_subject = subject.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))
                personalized_body = body.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))

                message_id = make_msgid(domain='dispatchskool.com')

                try:
                    
                    # Send email using EmailMultiAlternatives
                    msg = EmailMultiAlternatives(
                        subject=personalized_subject,
                        body=personalized_body,
                        from_email=email_account.email_address,
                        to=[lead['email']],
                        connection=connection,
                    )
                    msg.extra_headers = {'Message-ID': message_id}
                    msg.attach_alternative(personalized_body, "text/html")  # Attach HTML version
                    msg.send()

                    new_thread = EmailThread.objects.create(
                        subject=personalized_subject,
                        mailbox=mailbox,
                        email1=email_account.email_address, #sender
                        email2=lead['email'] #receiver
                    )

                    OutgoingEmailMessage.objects.create(
                        subject=personalized_subject,
                        body=personalized_body,
                        message_id=message_id,
                        sender=email_account.email_address,
                        recipient=lead['email'],
                        in_reply_to=None,  # It's not a reply, it's a first message
                        thread=new_thread  # Attach to the new thread
                    )

                    # Add the delay here before sending the next email
                    time.sleep(delay)
                except Exception as e:
                    print(f"Error sending email: {e}")

            print(f"Emails sent successfully to {len(leads)} recipients.")

            # Close the SMTP connection after sending
            connection.close()

            # Update last used timestamp
            email_account.last_used_at = now()
            email_account.save(update_fields=["last_used_at"])

        except Exception as e:
            print(f"Error in sending emails: {e}")

    # Execute the email sending process in a separate thread
    executor = ThreadPoolExecutor(max_workers=5)
    executor.submit(_send)


def process_excel_file(file):
    if not file.name.endswith('.xlsx'):
        return []
    
    try:
        df = pd.read_excel(file)
        
        normalized_columns = {col: col.strip().lower().replace(" ", "") for col in df.columns}
        column_mapping = {}
        expected_columns = {
            'mc_number': ['mc', 'mcnumber', 'mc_number', 'number'],
            'name': ['name', 'legalname'],
            'email': ['email', 'emailaddress']
        }
        
        for key, aliases in expected_columns.items():
            for col, norm_col in normalized_columns.items():
                if norm_col in aliases:
                    column_mapping[key] = col
                    break 

        if 'email' not in column_mapping or 'mc_number' not in column_mapping:
            return []

        # ✅ NEW: Helper to clean values (e.g., remove .0 from float or strip spaces)
        def clean_value(val):
            if pd.isnull(val):
                return ''
            if isinstance(val, float) and val.is_integer():
                return str(int(val))  # ✅ CHANGED: Prevents float to string issues
            return str(val).strip()

        def normalize_mc_number(val):
            val = clean_value(val)
            if not val.lower().startswith('mc'):
                return f"MC {val}"
            return val
        
        leads = [
            {
                'mc_number': normalize_mc_number(row[column_mapping['mc_number']]),
                'name': clean_value(row.get(column_mapping.get('name', ''), '')),
                'email': clean_value(row[column_mapping['email']])
            }
            for _, row in df.iterrows()
            if pd.notnull(row[column_mapping['email']])
        ]

        return leads
    except Exception as e:
        print(f"Error in process_excel_file: {e}")
        return []


def get_leads_from_db(starting_mc_number, targets_count):
    try:
        formatted_mc_number = f"MC {starting_mc_number}"
        starting_lead = Lead.objects.filter(mc_number__gte=formatted_mc_number).order_by('mc_number').first()
        
        if not starting_lead:
            starting_lead = Lead.objects.filter(mc_number__lte=formatted_mc_number).order_by('-mc_number').first()
        
        if not starting_lead:
            return []

        starting_mc = starting_lead.mc_number
        leads_after = list(Lead.objects.filter(mc_number__gte=starting_mc, email__isnull=False).exclude(email='').order_by('mc_number')[:targets_count])
        remaining = targets_count - len(leads_after)
        
        if remaining > 0:
            leads_before = list(Lead.objects.filter(mc_number__lt=starting_mc, email__isnull=False).exclude(email='').order_by('-mc_number')[:remaining])
            leads_after.extend(leads_before)

        return [{'mc_number': lead.mc_number, 'name': lead.legal_name, 'email': lead.email} for lead in leads_after if lead.email]
    except Exception as e:
        return []


@login_required
def campaign(request, email_account_id):
    
    email_account = get_object_or_404(EmailAccount, id=email_account_id, user=request.user)
    
    # Check if mailbox exists for this account
    mailbox = Mailbox.objects.filter(from_email=email_account.email_address).first()  # or whatever field links them
    
    if not mailbox:
        messages.error(request, "IMAP is not configured for this account. Please set it up before sending a campaign.")
        return redirect("dashboard:index")
    
    form = CampaignForm(user=request.user)

    if request.method == 'POST':
        form = CampaignForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            email_subject = form.cleaned_data['email_subject']
            email_body = form.cleaned_data['email_body']
            file_upload = form.cleaned_data['file_upload']
            mc_number = form.cleaned_data['mc_number']
            targets_count = form.cleaned_data['targets_count']
            delay = form.cleaned_data.get('delay') or 0  # default to 0 if not provided
            delay_unit = form.cleaned_data.get('delay_unit')

            # Convert delay to seconds if unit is in minutes
            if delay_unit == 'minutes':
                delay *= 60

            leads = []
            if file_upload:
                leads = process_excel_file(file_upload)
            elif mc_number and not request.user.on_free_trial:
                leads = get_leads_from_db(mc_number, targets_count)

            if not leads:
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:index')

            # Call send_emails function which already uses threading
            send_emails(email_account, leads, email_subject, email_body, delay, mailbox)

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

            leads = []
            if file_upload:
                leads = process_excel_file(file_upload)
            elif mc_number and not request.user.on_free_trial:
                leads = get_leads_from_db(mc_number, targets_count)

            if not leads:
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'error', 'message': 'No valid leads found.'})
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:bulk_campaign')
            
            # Before setting the cache, delete the old one
            cache.delete(cache_key)
            cache.set(cache_key, {'leads': leads, 'leads_available': len(leads)}, timeout=300)


            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'status': 'success',
                    'message': f'{len(leads)} leads submitted successfully.',
                    'leads': leads  # Include leads data here
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
            delay = form.cleaned_data.get('delay') or 0
            delay_unit = form.cleaned_data.get('delay_unit')
            if delay_unit == 'minutes':
                delay *= 60

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


            # Step 1: Filter accounts with and without mailbox
            valid_account_lead_map = {}
            failed_accounts = []

            for account, leads in account_lead_map.items():
                try:
                    if not account.imap_settings:
                        failed_accounts.append(account.email_address)
                    else:
                        valid_account_lead_map[account] = leads
                except:
                    continue

            print(valid_account_lead_map)
            print(failed_accounts)

            # Step 2: Define the thread
            def start_campaign():

                # Instead of using `in_bulk()`, we manually filter the mailboxes
                all_mailboxes = Mailbox.objects.filter(
                    from_email__in=[a.email_address for a in valid_account_lead_map]
                )
                # Create a dictionary to map email addresses to mailboxes
                mailbox_dict = {mailbox.from_email: mailbox for mailbox in all_mailboxes}

                with ThreadPoolExecutor(max_workers=min(5, len(valid_account_lead_map))) as executor:
                    for account, assigned_leads in valid_account_lead_map.items():
                        if assigned_leads:
                            mailbox = mailbox_dict.get(account.email_address)
                            executor.submit(
                                send_bulk_emails,
                                mailbox,
                                account,
                                assigned_leads,
                                email_subject,
                                email_body,
                                delay
                            )

            # Step 3: Start thread and show message
            if valid_account_lead_map:
                Thread(target=start_campaign).start()
                cache.delete(cache_key)

                success_emails = [acc.email_address for acc in valid_account_lead_map]
                message = f"🎉 Bulk Campaign started for:\n- " + "\n- ".join(success_emails)

                if failed_accounts:
                    message += "\n\n⚠️ Skipped due to missing IMAP:\n- " + "\n- ".join(failed_accounts)

                messages.success(request, message)
            else:
                messages.error(request, "❌ Campaign not started. None of the selected accounts have IMAP configured.")

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
    })



def send_bulk_emails(mailbox, email_account, leads, subject, body, delay):
    """Sends personalized emails using the given email account."""

    try:
        decrypted_password = email_account.get_password()

        use_tls = email_account.server_type == "TLS"
        use_ssl = email_account.server_type == "SSL"

        if use_tls and use_ssl:
            print("Invalid configuration: Cannot enable both TLS and SSL.")
            return

        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=email_account.host,
            port=email_account.port_number,
            username=email_account.email_address,
            password=decrypted_password,
            use_tls=use_tls,
            use_ssl=use_ssl,
        )

        try:
            connection.open()
        except Exception as e:
            print(f"Exception while opening connection: {e}")
            return

        for lead in leads:
            personalized_subject = subject.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))
            personalized_body = body.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))

            message_id = make_msgid(domain='dispatchskool.com')

            try:
                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=email_account.email_address,
                    to=[lead['email']],
                    connection=connection
                )
                msg.attach_alternative(personalized_body, "text/html")
                msg.send()

                new_thread = EmailThread.objects.create(
                    subject=personalized_subject,
                    mailbox=mailbox,
                    email1=email_account.email_address, #sender
                    email2=lead['email'] #receiver
                )

                OutgoingEmailMessage.objects.create(
                    subject=personalized_subject,
                    body=personalized_body,
                    message_id=message_id,
                    sender=email_account.email_address,
                    recipient=lead['email'],
                    in_reply_to=None,  # It's not a reply, it's a first message
                    thread=new_thread  # Attach to the new thread
                )

                time.sleep(delay)
            except Exception as e:
                print(f"Error sending email to {lead['email']}: {e}")
                continue

        connection.close()
        email_account.last_used_at = now()
        email_account.save(update_fields=["last_used_at"])

        print(f"✅ Emails sent using {email_account.email_address}")

    except Exception as e:
        print(f"❌ Error in sending emails: {e}")



######################################## Email accounts creation and dashboard views

@login_required
def index(request):
	email_accounts = EmailAccount.objects.filter(user=request.user).order_by('-last_used_at')
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

