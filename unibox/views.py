from django.shortcuts import render, get_object_or_404
from users.models import EmailAccount
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from dashboard.models import GmailToken
from unibox.models import EmailThread, OutgoingEmailMessage, IncomingEmailMessage, Attachment
import re
from django.db.models import Count, Max
from django.views.decorators.http import require_POST
from django.core.mail import get_connection, EmailMultiAlternatives
from email.utils import make_msgid
import uuid
from django.core.paginator import Paginator


@login_required
def inbox_page(request):
  return render(request, "unibox/index.html")


@login_required
def index(request):
    # Step 1: Fetch all mailbox addresses
    mailbox_addresses = GmailToken.objects.values_list('email_account__email_address', flat=True)

    # Step 2: Filter user's email accounts that are IMAP-configured and in Mailbox
    email_accounts = EmailAccount.objects.filter(
        user=request.user,
        email_address__in=mailbox_addresses
    )
    account_id = request.GET.get('account_id')
    page_number = request.GET.get('page', 1)
    threads_per_page = 25

    # Step 3: Get all mailbox instances corresponding to the user’s email accounts
    user_mailboxes = GmailToken.objects.filter(email_account__in=email_accounts.values_list('id', flat=True))

    # Step 4: Filter threads where the mailbox is the receiver (email2 = GmailToken.email_account)
    threads = EmailThread.objects.filter(email2__in=user_mailboxes.values_list('email_account__email_address', flat=True))

    # Step 5: Apply account_id filter if provided
    if account_id:
        try:
            selected_account = EmailAccount.objects.get(id=account_id, user=request.user)
            selected_mailbox_email = selected_account.email_address
            threads = threads.filter(email2=selected_mailbox_email)
        except EmailAccount.DoesNotExist:
            threads = threads.none()

    # Step 6: Annotate and filter threads where total message count > 0
    threads = threads.annotate(
        num_messages=Count('incoming_messages', distinct=True) + Count('outgoing_messages', distinct=True)
    ).filter(
        num_messages__gt=0
    )

    # Step 7: Annotate threads with the latest message timestamp and sort them
    threads = threads.annotate(
        latest_incoming_timestamp=Max('incoming_messages__received_at')
    ).order_by(
        '-latest_incoming_timestamp'
    ).select_related(
        'mailbox__email_account'
    ).prefetch_related(
        'incoming_messages',
        'outgoing_messages'
    )

    paginator = Paginator(threads, threads_per_page)
    page_obj = paginator.get_page(page_number)

    # Step 8: Unread counts for each mailbox
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

    # Step 9: Build email_accounts list with unread count
    email_accounts_data = [
        {
            "id": acc.id,
            "email_address": acc.email_address,
            "unread_count": unread_counts_dict.get(acc.id, 0)
        }
        for acc in email_accounts
    ]

    # Step 10: Build threads list
    threads_data = []
    for thread in page_obj.object_list:
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
            "mailbox_email": thread.mailbox.email_account.email_address,
            "email1": thread.email1,
            "email2": thread.email2,
            "messages": messages_data
        })

    # Final JSON response
    data = {
        "email_accounts": email_accounts_data,
        "threads": threads_data,
        "pagination": {
            "current_page": page_obj.number,
            "num_pages": paginator.num_pages,
            "has_previous": page_obj.has_previous(),
            "has_next": page_obj.has_next(),
        }
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
    # Get all email address ids the user owns
    user_email_account_ids = EmailAccount.objects.filter(user=request.user).values_list('id', flat=True)

    # Make sure the thread belongs to one of the user's mailboxes
    thread = get_object_or_404(
        EmailThread, 
        id=thread_id, 
        mailbox__email_account__id__in=user_email_account_ids
    )

    messages = thread.get_ordered_messages()
    serialized_messages = []
    updated_subject = thread.subject
    subject_prefixes = r"^(Re:|Fwd:|FW:|AW:|SV:)\s*"

    for msg in messages:
        # Build the attachments data for each message
        attachments_data = []
        for attachment in msg.attachments.all():
            attachments_data.append({
                "filename": attachment.filename,
                "size": attachment.size,
                "url": attachment.file.url
            })

        serialized_messages.append({
            "id": msg.id,
            "subject": msg.subject,
            "body": msg.body,
            "sender": msg.sender,
            "recipient": msg.recipient,
            "message_id": msg.message_id,
            "in_reply_to": msg.in_reply_to,
            "timestamp": msg._timestamp.isoformat() if msg._timestamp else None,
            "direction": "incoming" if hasattr(msg, 'received_at') else "outgoing",
            "attachments": attachments_data # Add the attachments data
        })
        if re.match(subject_prefixes, msg.subject, re.IGNORECASE) and not re.match(subject_prefixes, updated_subject, re.IGNORECASE):
            updated_subject = msg.subject

    if updated_subject != thread.subject:
        thread.subject = updated_subject
        thread.save()

    # Find the associated email account for this thread's mailbox
    try:
        email_account = EmailAccount.objects.get(email_address=thread.mailbox.email_account.email_address, user=request.user)
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


@require_POST
@login_required
def reply(request):
    thread_id = request.POST.get('thread_id')
    body = request.POST.get('body')
    recipient_email = request.POST.get('recipient_email')
    
    # Access the file from request.FILES
    uploaded_file = request.FILES.get('attachment')

    if not thread_id or not body or not recipient_email:
        return JsonResponse({'status': 'error', 'message': 'Missing required data.'}, status=400)

    try:
        thread = get_object_or_404(EmailThread, id=thread_id)

        # Verify the thread belongs to the current user's mailbox
        user_email_accounts = EmailAccount.objects.filter(user=request.user)
        if not user_email_accounts.filter(email_address=thread.mailbox.email_account.email_address).exists():
            return JsonResponse({'status': 'error', 'message': 'Permission denied for this thread.'}, status=403)

        email_account = thread.mailbox.email_account
        sender_email = email_account.email_address

        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=email_account.host,
            port=email_account.port_number,
            username=sender_email,
            password=email_account.get_password(),
            use_tls=email_account.server_type == "TLS",
            use_ssl=email_account.server_type == "SSL",
        )

        try:
            connection.open()
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f'Could not connect to SMTP server: {e}'}, status=500)

        subject = f"Re: {thread.subject}" if not thread.subject.startswith("Re:") else thread.subject
        to_email = [recipient_email]
        message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')

        latest_incoming = thread.incoming_messages.order_by('-received_at').first()
        in_reply_to = latest_incoming.message_id if latest_incoming else None

        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=sender_email,
            to=to_email,
            connection=connection
        )
        msg.extra_headers = {
            'Message-ID': message_id,
            'In-Reply-To': in_reply_to,
            'References': in_reply_to,
        }

        # Attach the file if it was uploaded
        if uploaded_file:
            msg.attach(uploaded_file.name, uploaded_file.read(), uploaded_file.content_type)

        msg.send()
        connection.close()

        # Save the outgoing message
        outgoing_message = OutgoingEmailMessage.objects.create(
            thread=thread,
            subject=subject,
            body=body,
            recipient=recipient_email,
            sender=sender_email,
            message_id=message_id,
            in_reply_to=in_reply_to
        )

        # Save the attachment model instance if a file was uploaded
        if uploaded_file:
            Attachment.objects.create(
                outgoing_message=outgoing_message,
                file=uploaded_file,
                filename=uploaded_file.name,
                mime_type=uploaded_file.content_type,
                size=uploaded_file.size
            )

        return JsonResponse({'status': 'success'})

    except EmailAccount.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Email account not found.'}, status=404)
    except EmailThread.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Thread not found.'}, status=404)
    except Exception as e:
        if 'authentication failed' in str(e).lower():
            return JsonResponse({'status': 'error', 'message': 'Authentication failed.'}, status=401)
        return JsonResponse({'status': 'error', 'message': f'Failed to send reply: {e}'}, status=500)

