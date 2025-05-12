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
from django.db.models import Count, Max
from django.db.models.functions import Greatest
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
    # Step 1: Fetch all mailbox addresses
    mailbox_addresses = Mailbox.objects.values_list('from_email', flat=True)

    # Step 2: Filter user's email accounts that are IMAP-configured and in Mailbox
    email_accounts = EmailAccount.objects.filter(
        user=request.user,
        email_address__in=mailbox_addresses
    )
    account_id = request.GET.get('account_id')

    # Step 3: Get all mailbox instances corresponding to the user’s email accounts
    user_mailboxes = Mailbox.objects.filter(from_email__in=email_accounts.values_list('email_address', flat=True))

    # Step 4: Filter threads where the mailbox is the receiver (email2 = mailbox.from_email)
    threads = EmailThread.objects.filter(email2__in=user_mailboxes.values_list('from_email', flat=True))

    # Step 5: Apply account_id filter if provided
    if account_id:
        try:
            selected_account = EmailAccount.objects.get(id=account_id, user=request.user)
            selected_mailbox_email = selected_account.email_address
            threads = threads.filter(email2=selected_mailbox_email)
        except EmailAccount.DoesNotExist:
            threads = threads.none()

    # Annotate threads with the latest message timestamp.  This is the key change.
    threads = threads.annotate(
        latest_incoming_timestamp=Max('incoming_messages__received_at')
    ).order_by('is_read', '-latest_incoming_timestamp') # Order by the latest timestamp


    # Step 6: Unread counts for each mailbox
    unread_counts = EmailThread.objects.filter(
        email2__in=email_accounts.values_list('email_address', flat=True),
        is_read=False
    ).values('email2').annotate(count=Count('id'))


    # Map email address to account ID
    email_to_account_id = {acc.email_address: acc.id for acc in email_accounts}
    unread_counts_dict = {
        email_to_account_id.get(item['email2']): item['count']
        for item in unread_counts if email_to_account_id.get(item['email2']) is not None
    }

    # Step 7: Build email_accounts list with unread count
    email_accounts_data = [
        {
            "id": acc.id,
            "email_address": acc.email_address,
            "unread_count": unread_counts_dict.get(acc.id, 0)
        }
        for acc in email_accounts
    ]

    # Step 8: Build threads list
    threads_data = []
    for thread in threads.select_related('mailbox'):
        messages = thread.get_ordered_messages()
        messages_data = []

        for msg in messages:
            is_incoming = isinstance(msg, IncomingEmailMessage)
            messages_data.append({
                "id": msg.id,
                "subject": msg.subject,
                "body": msg.body,
                "sender": msg.sender,
                "recipient": msg.recipient,
                "message_id": msg.message_id,
                "in_reply_to": msg.in_reply_to,
                "timestamp": getattr(msg, 'received_at', None) if is_incoming else getattr(msg, 'sent_at', None),
                "type": "incoming" if is_incoming else "outgoing"
            })

        threads_data.append({
            "id": thread.id,
            "subject": thread.subject,
            "created_at": thread.created_at,
            "is_read": thread.is_read,
            "mailbox_email": thread.mailbox.from_email,
            "email1": thread.email1,
            "email2": thread.email2,
            "messages": messages_data
        })

    # Final JSON response
    data = {
        "email_accounts": email_accounts_data,
        "threads": threads_data
    }

    return JsonResponse(data, safe=False)


@login_required
def toggle_read_status(request, thread_id):
    if request.method == 'POST':
        try:
            thread = get_object_or_404(EmailThread, id=thread_id)
            thread.is_read = not thread.is_read
            thread.save()
            return JsonResponse({'status': 'success', 'is_read': thread.is_read})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    else:
        return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)



@login_required
def get_thread_messages(request, thread_id):
    # Get all email addresses the user owns
    user_email_addresses = EmailAccount.objects.filter(user=request.user).values_list('email_address', flat=True)

    # Make sure the thread belongs to one of the user's mailboxes
    thread = get_object_or_404(EmailThread, id=thread_id, mailbox__from_email__in=user_email_addresses)

    messages = thread.get_ordered_messages()
    serialized_messages = []
    updated_subject = thread.subject
    subject_prefixes = r"^(Re:|Fwd:|FW:|AW:|SV:)\s*"

    for msg in messages:
        serialized_messages.append({
            "id": msg.id,
            "subject": msg.subject,
            "body": msg.body,
            "sender": msg.sender,
            "recipient": msg.recipient,
            "message_id": msg.message_id,
            "in_reply_to": msg.in_reply_to,
            "timestamp": msg._timestamp.isoformat() if msg._timestamp else None,
            "direction": "incoming" if hasattr(msg, 'received_at') else "outgoing"
        })
        if re.match(subject_prefixes, msg.subject, re.IGNORECASE) and not re.match(subject_prefixes, updated_subject, re.IGNORECASE):
            updated_subject = msg.subject

    if updated_subject != thread.subject:
        thread.subject = updated_subject
        thread.save()

    # Find the associated email account for this thread's mailbox
    try:
        email_account = EmailAccount.objects.get(email_address=thread.mailbox.from_email, user=request.user)
        email_account_id = email_account.id
    except EmailAccount.DoesNotExist:
        email_account_id = None

    data = {
        "id": thread.id,
        "subject": thread.subject,
        "messages": serialized_messages,
        "email_account_id": email_account_id,
    }
    return JsonResponse(data)


@login_required
def delete_thread(request, thread_id):
    if request.method == 'POST':
        try:
            thread = get_object_or_404(EmailThread, id=thread_id)

            # Only allow if the thread email2 matches one of the user's accounts
            user_emails = EmailAccount.objects.filter(user=request.user).values_list('email_address', flat=True)
            if thread.email2 not in user_emails:
                return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)

            thread.delete()
            return JsonResponse({'status': 'success', 'message': 'Thread deleted'})

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)


@login_required
@require_POST
def reply(request):
    
    if request.method == 'POST':
        thread_id = request.POST.get('thread_id')
        body = request.POST.get('body')
        recipient_email = request.POST.get('recipient_email')

        if not thread_id or not body or not recipient_email:
            return JsonResponse({'status': 'error', 'message': 'Missing required data.'}, status=400)

        try:
            thread = get_object_or_404(EmailThread, id=thread_id)

            # Verify the thread belongs to the current user's mailbox
            user_email_addresses = EmailAccount.objects.filter(user=request.user).values_list('email_address', flat=True)
            if thread.mailbox.from_email not in user_email_addresses:
                return JsonResponse({'status': 'error', 'message': 'Permission denied for this thread.'}, status=403)

            # Determine the sender of the reply
            email_account = EmailAccount.objects.get(email_address=thread.mailbox.from_email, user=request.user)

            # SMTP credentials and connection
            decrypted_password = email_account.get_password()
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
                return JsonResponse({'status': 'error', 'message': f'Could not connect to SMTP server: {e}'}, status=500)

            subject = f"{thread.subject}"
            from_email = email_account.email_address
            to_email = [recipient_email]
            message_id = make_msgid(domain='dispatchskool.com/')

            latest_incoming = thread.incoming_messages.order_by('-received_at').first()
            in_reply_to = latest_incoming.message_id if latest_incoming else None

            msg = EmailMultiAlternatives(subject=subject, body=body, from_email=from_email, to=to_email, connection=connection)
            msg.attach_alternative(body, "text/html")
            msg.extra_headers = {'Message-ID': message_id}
            if in_reply_to:
                msg.extra_headers['In-Reply-To'] = in_reply_to
                msg.extra_headers['References'] = in_reply_to

            msg.send()
            connection.close()

            # Save the outgoing message
            OutgoingEmailMessage.objects.create(
                thread=thread,
                subject=subject,
                body=body,
                recipient=recipient_email,
                sender=from_email,
                message_id=message_id,
                in_reply_to=in_reply_to
            )

            return JsonResponse({'status': 'success'})

        except EmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Email account not found.'}, status=404)
        except EmailThread.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Thread not found.'}, status=404)
        except Exception as e:
            if 'authentication failed' in str(e).lower():
                return JsonResponse({'status': 'error', 'message': f'Authentication failed: {e}'}, status=401)
            return JsonResponse({'status': 'error', 'message': f'Failed to send reply: {e}'}, status=500)
    else:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

