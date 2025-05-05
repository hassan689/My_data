from django.shortcuts import render, get_object_or_404, redirect
from users.models import EmailAccount
from .forms import IMAPSettingsForm
from django.contrib import messages
from django_mailbox.models import Mailbox
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from dashboard.models import OutgoingEmailMessage, IncomingEmailMessage
from unibox.models import EmailThread
import re
from django.db.models import Count, F, Q, Exists, OuterRef
from django.views.decorators.http import require_POST
from django.core.mail import get_connection, EmailMultiAlternatives
from email.utils import make_msgid




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
    
    mailbox_addresses = Mailbox.objects.values_list('from_email', flat=True)

    # Filter user's email accounts that are IMAP-configured and in Mailbox
    email_accounts = EmailAccount.objects.filter(
        user=request.user,
        has_imap_configured=True,
        email_address__in=mailbox_addresses
    )
    account_id = request.GET.get('account_id')
    threads = EmailThread.objects.filter(email_account__in=email_accounts).annotate(
        num_incoming=Count('incoming_messages'),
        num_outgoing=Count('outgoing_messages'),
        total_messages=F('num_incoming') + F('num_outgoing'),
        has_first_incoming=Exists(
            IncomingEmailMessage.objects.filter(thread=OuterRef('id')).order_by('received_at').values('id')[:1]
        ),
        has_first_outgoing=Exists(
            OutgoingEmailMessage.objects.filter(thread=OuterRef('id')).order_by('sent_at').values('id')[:1]
        )
    ).filter(
        Q(has_first_incoming=True) | Q(total_messages__gt=1)
    ).exclude(
        Q(has_first_outgoing=True) & Q(total_messages__lt=2)
    )

    if account_id:
        threads = threads.filter(email_account_id=account_id)
    unread_counts = {}
    total_unread_count = 0

    for acc in email_accounts:
        unread_count = threads.filter(email_account=acc, is_read=False).count()
        unread_counts[acc.id] = unread_count
        total_unread_count += unread_count

    data = {
        "email_accounts": [
            {"id": acc.id, "email_address": acc.email_address, "has_imap_configured": acc.has_imap_configured, "unread_count": unread_counts.get(acc.id, 0)}
            for acc in email_accounts
        ],
        "threads": [
            {
                "id": thread.id,
                "subject": thread.subject,
                "email_account_id": thread.email_account.id,
                "started_at": thread.started_at.isoformat(),
                "is_read": thread.is_read,
                "messages": [
                    {
                        "id": msg["id"], "subject": msg["subject"], "body": msg["body"],
                        "sender": msg["sender"], "recipient": msg["recipient"], "message_id": msg["message_id"],
                        "in_reply_to": msg["in_reply_to"],
                        "timestamp": msg["timestamp"].isoformat() if msg["timestamp"] else None,
                        "direction": msg["direction"]
                    }
                    for msg in thread.get_ordered_messages()
                ]
            }
            for thread in threads
        ],
        "total_unread_count": total_unread_count,
    }
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


@login_required
@require_POST
def unibox_reply_view(request):
    thread_id = request.POST.get('thread_id')
    body = request.POST.get('body')
    recipient_email = request.POST.get('recipient')
    print(f"Recipient Email: {recipient_email}")

    if not thread_id or not body or not recipient_email:
        return JsonResponse({'status': 'error', 'message': 'Missing required data.'}, status=400)

    try:
        thread = get_object_or_404(EmailThread, id=thread_id)
        email_account = thread.email_account

        # Decrypt the stored password
        decrypted_password = email_account.get_password()

        # Determine SMTP security type
        use_tls = email_account.server_type == "TLS"
        use_ssl = email_account.server_type == "SSL"

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
            return JsonResponse({'status': 'error', 'message': f'Could not connect to SMTP server: {e}'}, status=500)

        # Construct the email message
        subject = f"Re: {thread.subject}"
        from_email = email_account.email_address
        to_email = [recipient_email]
        message_id = make_msgid(domain='dispatchskool.com/')

        # Get the message_id of the latest incoming message for In-Reply-To
        latest_incoming = thread.incoming_messages.order_by('-received_at').first()
        in_reply_to = latest_incoming.message_id if latest_incoming else None

        msg = EmailMultiAlternatives(subject=subject, body=body, from_email=from_email, to=to_email, connection=connection)
        msg.attach_alternative(body, "text/html")
        msg.extra_headers = {'Message-ID': message_id}
        if in_reply_to:
            msg.extra_headers['In-Reply-To'] = in_reply_to
            msg.extra_headers['References'] = in_reply_to # For a simple reply, References can often be the same as In-Reply-To

        print(msg)
        print(msg.send())

        # Save the outgoing message
        OutgoingEmailMessage.objects.create(
            email_account=email_account,
            subject=subject,
            body=body,
            recipient=recipient_email,
            sender=from_email,
            message_id=message_id,
            thread=thread,
            in_reply_to=in_reply_to
        )

        connection.close()
        return JsonResponse({'status': 'success'})

    except EmailAccount.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Email account not found for this thread.'}, status=404)
    except EmailThread.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Thread not found.'}, status=404)
    except Exception as e:
        print(f"Error sending reply: {e}")
        if 'authentication failed' in str(e).lower():
            return JsonResponse({'status': 'error', 'message': f'Authentication failed while sending email: {e}'}, status=401)
        return JsonResponse({'status': 'error', 'message': f'Failed to send reply: {e}'}, status=500)

