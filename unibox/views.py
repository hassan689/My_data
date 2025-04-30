from django.shortcuts import render, get_object_or_404, redirect
from users.models import EmailAccount
from .forms import IMAPSettingsForm
from django.contrib import messages
from django_mailbox.models import Mailbox
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from unibox.models import EmailThread

@login_required
def add_imap_settings(request, email_account_id):
    
    email_account = get_object_or_404(EmailAccount, id=email_account_id)
    form = IMAPSettingsForm()
    
    # Check if this email account belongs to the logged-in user
    if email_account.user != request.user:
        messages.error(request, "You do not have permission to add IMAP settings for this account.")
        return redirect('dashboard:index')
    
    if request.method == 'POST':
        form = IMAPSettingsForm(request.POST)
        if form.is_valid():
            imap_settings = form.save(commit=False)
            imap_settings.email_account = email_account
            email_account.has_imap_configured = True
            email_account.save()
            imap_settings.save()

            # Method to create their mailbox
            decrypted_password = email_account.get_password()

            # Build IMAP URI based on encryption type
            if imap_settings.imap_encryption == "SSL":
                protocol = "imap+ssl"
            else:  # TLS
                protocol = "imap+tls"

            uri = f"{protocol}://{email_account.email_address}:{decrypted_password}@{imap_settings.imap_host}:{imap_settings.imap_port}"

            # Optional: delete old mailbox with same name
            Mailbox.objects.filter(name=f"Mailbox-{email_account.id}").delete()

            # Create new Mailbox
            Mailbox.objects.create(
                name = f"Mailbox-{email_account.id}",
                uri = uri,
                from_email = email_account.email_address,
                active = True
            )

            messages.success(request, "IMAP settings have been successfully saved.")
            return redirect('dashboard:index')  # adjust to your desired success URL
    else:
        form = IMAPSettingsForm()

    return render(request, 'unibox/imapsettings_form.html', {'form': form, 'email_account': email_account})


@login_required
def inbox_page(request):
  return render(request, "unibox/index.html")


@login_required
def index(request):
    # Retrieve email addresses from Mailbox objects
    mailbox_addresses = Mailbox.objects.values_list('from_email', flat=True)

    threads = EmailThread.objects.filter(
        email_account__user=request.user,
        email_account__has_imap_configured=True,
        email_account__email_address__in=mailbox_addresses
    )
    data = [
        {
            "id": thread.id,
            "subject": thread.subject,
            "started_at": thread.started_at,
            "messages": list(thread.get_ordered_messages())
        }
        for thread in threads
    ]
    return JsonResponse({"threads": data})


