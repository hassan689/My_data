from django.shortcuts import render, get_object_or_404, redirect
from users.models import EmailAccount
from .forms import IMAPSettingsForm
from django.contrib import messages
from django_mailbox.models import Mailbox
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from unibox.models import EmailThread
import re  # Import the regular expression module
from django.db.models import Count, F, Value, CharField

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
    # Get all Mailbox email addresses
    mailbox_addresses = Mailbox.objects.values_list('from_email', flat=True)

    # Filter user's email accounts that are IMAP-configured and in Mailbox
    email_accounts = EmailAccount.objects.filter(
        user=request.user,
        has_imap_configured=True,
        email_address__in=mailbox_addresses
    )

    # Annotate EmailThread with the total number of messages
    threads = EmailThread.objects.filter(email_account__in=email_accounts).annotate(
        num_incoming=Count('incoming_messages'),
        num_outgoing=Count('outgoing_messages'),
        total_messages=F('num_incoming') + F('num_outgoing')
    ).filter(total_messages__gt=1)  # Filter for threads with more than 1 message

    data = {
        "email_accounts": [
            {
                "id": acc.id,
                "email_address": acc.email_address,
                "has_imap_configured": acc.has_imap_configured
            }
            for acc in email_accounts
        ],
        "threads": []
    }

    for thread in threads:
        messages = thread.get_ordered_messages()
        serialized_messages = []
        for msg in messages:
            serialized_messages.append({
                "id": msg["id"],
                "subject": msg["subject"],
                "body": msg["body"],
                "sender": msg["sender"],
                "recipient": msg["recipient"],
                "message_id": msg["message_id"],
                "in_reply_to": msg["in_reply_to"],
                "timestamp": msg["timestamp"].isoformat() if msg["timestamp"] else None,
                "direction": msg["direction"]
            })

        data["threads"].append({
            "id": thread.id,
            "subject": thread.subject,
            "is_read": thread.is_read,  # Include the is_read status
            "email_account_id": thread.email_account.id,
            "started_at": thread.started_at.isoformat(),
            "messages": serialized_messages
        })

    return JsonResponse(data)


@login_required
def mark_thread_read(request, thread_id):
    thread = get_object_or_404(EmailThread, id=thread_id, email_account__user=request.user)
    if request.method == 'POST':
        is_read_str = request.POST.get('is_read')
        if is_read_str is not None:
            is_read = is_read_str.lower() == 'true'
            thread.is_read = is_read
            thread.save()
            return JsonResponse({'status': 'success', 'is_read': thread.is_read, 'message': f'Thread {thread_id} read status updated to {thread.is_read}'})
        else:
            return JsonResponse({'status': 'error', 'message': 'Missing "is_read" parameter'}, status=400)
    else:
        return JsonResponse({'status': 'error', 'message': 'Only POST requests are allowed'}, status=405)


@login_required
def get_thread_messages(request, thread_id):
    thread = get_object_or_404(EmailThread, id=thread_id, email_account__user=request.user)
    messages = thread.get_ordered_messages()
    serialized_messages = []
    updated_subject = thread.subject
    subject_prefixes = r"^(Re:|Fwd:|FW:|AW:|SV:)\s*" # Regular expression to match common prefixes

    for msg in messages:
        serialized_messages.append({
            "id": msg["id"],
            "subject": msg["subject"],
            "body": msg["body"],
            "sender": msg["sender"],
            "recipient": msg["recipient"],
            "message_id": msg["message_id"],
            "in_reply_to": msg["in_reply_to"],
            "timestamp": msg["timestamp"].isoformat() if msg["timestamp"] else None,
            "direction": msg["direction"]
        })
        # Check if the message subject starts with a common reply/forward prefix
        if re.match(subject_prefixes, msg["subject"], re.IGNORECASE) and not re.match(subject_prefixes, updated_subject, re.IGNORECASE):
            updated_subject = msg["subject"]

    # Update the thread subject if it has changed
    if updated_subject != thread.subject:
        thread.subject = updated_subject
        thread.save()

    data = {
        "id": thread.id,
        "subject": thread.subject,
        "messages": serialized_messages,
        "email_account_id": thread.email_account.id,
    }
    return JsonResponse(data)


