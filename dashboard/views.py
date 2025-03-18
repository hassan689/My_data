from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from users.models import EmailAccount
from .forms import EmailAccountForm, CampaignForm
from leads_data.models import Lead
import pandas as pd
from django.utils.timezone import now
from django.contrib import messages
from django.core.mail import get_connection, EmailMultiAlternatives
from concurrent.futures import ThreadPoolExecutor


######################################## Campaign sending views


def send_emails(email_account, leads, subject, body):
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
            connection.open()

            # Send personalized emails
            for lead in leads:
                personalized_subject = subject.replace("[name]", lead['name']).replace("[mc_number]", lead['mc_number'])
                personalized_body = body.replace("[name]", lead['name']).replace("[mc_number]", lead['mc_number'])

                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=email_account.email_address,
                    to=[lead['email']],
                    connection=connection
                )
                msg.attach_alternative(personalized_body, "text/html")  # Attach the HTML version
                msg.send()

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
        
        column_mapping = {}
        expected_columns = {'mc_number': ['mc', 'mc number', 'MC Number', 'MCNumber'], 'name': ['name', 'legal name', 'legalname'], 'email': ['email', 'email address', 'emailaddress']}
        
        for col in df.columns:
            col_lower = col.lower()
            for key, aliases in expected_columns.items():
                if any(alias in col_lower for alias in aliases):
                    column_mapping[key] = col
        
        if 'email' not in column_mapping:
            return []
        
        leads = [
            {'mc_number': row[column_mapping['mc_number']], 'name': row.get(column_mapping.get('name', ''), ''), 'email': row[column_mapping['email']]}
            for _, row in df.iterrows()
            if pd.notnull(row[column_mapping['email']])
        ]
        return leads
    except Exception as e:
        return []


def get_leads_from_db(starting_mc_number):
    try:
        formatted_mc_number = f"MC {starting_mc_number}"
        starting_lead = Lead.objects.filter(mc_number__gte=formatted_mc_number).order_by('mc_number').first()
        
        if not starting_lead:
            starting_lead = Lead.objects.filter(mc_number__lte=formatted_mc_number).order_by('-mc_number').first()
        
        if not starting_lead:
            return []

        starting_mc = starting_lead.mc_number
        leads_after = list(Lead.objects.filter(mc_number__gte=starting_mc).order_by('mc_number')[:300])
        remaining = 300 - len(leads_after)
        
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

            leads = []
            if file_upload:
                leads = process_excel_file(file_upload)
            elif mc_number and not request.user.on_free_trial:
                leads = get_leads_from_db(mc_number)

            if not leads:
                messages.error(request, "No valid leads found.")
                return redirect('dashboard:index')
            else:
                print(leads)

            # Call send_emails function which already uses threading
            send_emails(email_account, leads, email_subject, email_body)

            messages.success(request, f"Success! Emails are being sent for {email_account.email_address}. thank you for your patience")
            return redirect('dashboard:index')

    else:
        form = CampaignForm(user=request.user)

    return render(request, 'dashboard/campaign.html', {'form': form, 'email_account': email_account})




######################################## Email accounts creation and dashboard views

@login_required
def index(request):
	email_accounts = EmailAccount.objects.filter(user=request.user, is_active=True)
	context = {
		"email_accounts": email_accounts
	}
	return render(request, 'dashboard/index.html', context)


@login_required
def add_email_account(request):
    
    form = EmailAccountForm()
    if request.method == "POST":
        form = EmailAccountForm(request.POST)
        if form.is_valid():
            email_account = form.save(commit=False)
            email_account.user = request.user  # Assign authenticated user
            email_account.save()
            return redirect("dashboard:index")
    else:
        form = EmailAccountForm()
    context = {
        "form": form
    }
    return render(request, 'dashboard/add_email_account.html', context)


# Update Email Account
@login_required
def email_account_update(request, id):
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    form = EmailAccountForm(instance=email_account)
    
    if request.method == "POST":
        form = EmailAccountForm(request.POST, instance=email_account)
        if form.is_valid():
            form.save()
            return redirect("dashboard:index")
    else:
        form = EmailAccountForm(instance=email_account)
    
    return render(request, "dashboard/add_email_account.html", {"form": form})

# Soft Delete (Deactivate)
@login_required
def email_account_delete(request, id):
    
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    email_account.delete()
    return redirect("dashboard:index")

