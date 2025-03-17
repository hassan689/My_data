from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from users.models import EmailAccount
from .forms import EmailAccountForm, CampaignForm
from django.http import JsonResponse
from leads_data.models import Lead
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor
from django.utils.timezone import now


######################################## Campaign sending views


def send_emails(email_account, recipients, subject, body):
    """Logs in once and sends multiple emails efficiently."""
    try:
        # Prepare email connection
        context = ssl.create_default_context()
        server = None

        if email_account.server_type == "TLS":
            server = smtplib.SMTP(email_account.host, email_account.port_number)
            server.starttls(context=context)
        elif email_account.server_type == "SSL":
            server = smtplib.SMTP_SSL(email_account.host, email_account.port_number, context=context)

        # Login once
        server.login(email_account.email_address, email_account.encrypted_password)

        def send_single_email(recipient):
            """Function to send a single email."""
            try:
                msg = MIMEMultipart()
                msg["From"] = email_account.email_address
                msg["To"] = recipient
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))

                server.sendmail(email_account.email_address, recipient, msg.as_string())
                print(f"Email sent successfully to {recipient}")
            except Exception as e:
                print(f"Failed to send email to {recipient}: {e}")

        # Use ThreadPoolExecutor to send multiple emails in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(send_single_email, recipients)

        # Close the server connection after sending all emails
        server.quit()

        # Update last used timestamp
        email_account.last_used_at = now()
        email_account.save(update_fields=["last_used_at"])

    except Exception as e:
        print(f"Error in sending emails: {e}")


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
                return JsonResponse({'error': 'No valid leads found.'}, status=400)

            # Call send_emails function which already uses threading
            send_emails(email_account, [lead['email'] for lead in leads], email_subject, email_body)

            return JsonResponse({'success': 'Emails are being sent.'})

    else:
        form = CampaignForm(user=request.user)

    return render(request, 'dashboard/campaign.html', {'form': form})




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

