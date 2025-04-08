from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from users.models import EmailAccount
from .forms import EmailAccountForm, CampaignForm
from leads_data.models import Lead, DailySheet
import pandas as pd
from django.utils.timezone import now
from django.contrib import messages
from django.core.mail import get_connection, EmailMultiAlternatives, EmailMessage
from concurrent.futures import ThreadPoolExecutor
from django.core.exceptions import ValidationError
import time

######################################## Campaign sending views


def send_emails(request, email_account, leads, subject, body, delay):
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

                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=email_account.email_address,
                    to=[lead['email']],
                    connection=connection
                )
                msg.attach_alternative(personalized_body, "text/html")  # Attach the HTML version
                msg.send()

                # Add the delay here before sending the next email
                time.sleep(delay)  # delay in seconds, whether from minutes or directly in seconds

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
        leads_after = list(Lead.objects.filter(mc_number__gte=starting_mc).order_by('mc_number')[:targets_count])
        remaining = targets_count - len(leads_after)
        
        if remaining > 0:
            leads_before = list(Lead.objects.filter(mc_number__lt=starting_mc).order_by('-mc_number')[:remaining])
            leads_after.extend(leads_before)

        return [{'mc_number': lead.mc_number, 'name': lead.legal_name, 'email': lead.email} for lead in leads_after if lead.email]
    except Exception as e:
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
            else:
                print(leads)

            # Call send_emails function which already uses threading
            send_emails(request, email_account, leads, email_subject, email_body, delay)

            messages.success(request, f"Success! Emails are being sent for {email_account.email_address}. Thank you for your patience.")
            return redirect('dashboard:index')

    else:
        form = CampaignForm(user=request.user)

    return render(request, 'dashboard/campaign.html', {'form': form, 'email_account': email_account})


######################################## Email accounts creation and dashboard views

@login_required
def index(request):
	email_accounts = EmailAccount.objects.filter(user=request.user)
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
                    "If your email credentials were entered correctly and registration was successful, "
                    "you should receive a confirmation email in a couple of minutes.\n\n"
                    "- If you receive the email—great! Your registration was successful.\n"
                    "- If not, please review the registration guidelines and try again.\n\n"
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
                    "If your email credentials were entered correctly and registration was successful, "
                    "you should receive a confirmation email in a couple of minutes.\n\n"
                    "- If you receive the email—great! Your registration was successful.\n"
                    "- If not, please review the registration guidelines and try again.\n\n"
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

