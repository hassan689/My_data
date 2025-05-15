from django.core.management import call_command
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor
from django_mailbox.models import Mailbox, Message
from dashboard.models import IncomingEmailMessage, OutgoingEmailMessage
from unibox.models import EmailThread
from django.contrib.postgres.search import TrigramSimilarity
from email.utils import parseaddr
import re
from django.core.mail import send_mail
from django.conf import settings
import threading
import imaplib


def extract_reply_only(body):
    """
    Extracts the main part of an email body, removing quoted replies.

    Args:
        body (str): The full email body.

    Returns:
        str: The extracted reply content.
    """
    quote_patterns = [
        r"On\s.+?wrote:",  # Matches "On [date] wrote:"
        r"From:\s.+",      # Matches "From: <email>"
        r"Sent:\s.+",      # Matches "Sent: <date>"
        r">",              # Matches quoted lines
    ]
    combined_pattern = re.compile("|".join(quote_patterns), re.IGNORECASE)
    match = combined_pattern.search(body)
    if match:
        return body[:match.start()].strip()
    return body.strip()


def send_unread_notification(mailbox_email):
    """
    Sends an email notification to a mailbox about new unread messages.

    Args:
        mailbox_email (str): The email address of the mailbox.
    """
    subject = "New Unread Messages"
    message = "You have new unread messages in your Dispatch Skool mail boxes. Please log in to view them."
    from_email = settings.EMAIL_HOST_USER
    recipient_list = [mailbox_email]

    try:
        send_mail(subject, message, from_email, recipient_list, fail_silently=False)
        print(f"Unread notification email sent to {mailbox_email}")
    except Exception as e:
        print(f"Error sending unread notification email to {mailbox_email}: {e}")


def process_mailbox(mailbox):
    """
    Processes incoming emails for a specific mailbox and organizes them into threads.

    Args:
        mailbox (Mailbox): The Mailbox instance to process.
    """
    print(f"called for {mailbox}")

    # Step 3: Process only messages related to this mailbox
    messages = Message.objects.filter(mailbox=mailbox)
    print(messages)
    to_delete_ids = []  # Track IDs of messages to delete later
    processed_count = 0  # Count of processed messages
    deleted_count = 0  # Count of deleted messages
    new_messages_created = False  # Flag to track if new messages were created

    for msg in messages:
        try:
            email_obj = msg.get_email_object()
            in_reply_to_header = email_obj.get('In-Reply-To')
            rfrncs_header = email_obj.get('References')

            subject = msg.subject or ''
            body = None

            if email_obj.is_multipart():
                for part in email_obj.walk():
                    content_type = part.get_content_type()
                    content_disposition = part.get("Content-Disposition", "")
                    transfer_encoding = part.get("Content-Transfer-Encoding", "").lower()

                    if content_type == "text/plain" and "attachment" not in content_disposition:
                        payload = part.get_payload(decode=True)
                        if payload:
                            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            body = extract_reply_only(decoded)
                            break
            else:
                payload = email_obj.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(email_obj.get_content_charset() or "utf-8", errors="replace")
                    body = extract_reply_only(decoded)

            sender_tuple = parseaddr(msg.from_address or '')
            sender = sender_tuple[1] or ''
            received_at = msg.processed or timezone.now()

            # our mailbox is the recipient
            recipient = mailbox.from_email

            # [Self-loop Duplicate Detection]
            try:
                outgoing_match = OutgoingEmailMessage.objects.get(message_id=msg.message_id)
                print("Detected looped-back message sent from another internal mailbox")

                # Flip sender and recipient for perspective correction
                thread, _ = EmailThread.objects.get_or_create(
                    mailbox=mailbox,
                    email2=sender,
                    email1=recipient,
                    subject=subject
                )

                IncomingEmailMessage.objects.create(
                    thread=thread,
                    subject=subject,
                    body=body,
                    sender=recipient,  # Flip
                    recipient=sender,  # Flip
                    message_id=msg.message_id,
                    in_reply_to=in_reply_to_header,
                    received_at=received_at
                )
                new_messages_created = True  # Set the flag
                Message.objects.get(id=msg.id).delete()
                # to_delete_ids.append(msg.id)
                processed_count += 1
                continue  # Skip rest of logic for this case
            except OutgoingEmailMessage.DoesNotExist:
                pass  # Not a looped-back message, proceed normally

            thread = None
            outgoing_msg = None

            # Try to find the matching outgoing message using In-Reply-To
            if in_reply_to_header:
                outgoing_msg = OutgoingEmailMessage.objects.filter(message_id=in_reply_to_header).first()

            # Fallback: try using the last message_id from References header
            if not outgoing_msg and rfrncs_header:
                # Extract the last message ID from the References header
                message_ids = rfrncs_header.strip().split()
                if message_ids:
                    last_reference_id = message_ids[-1]
                    outgoing_msg = OutgoingEmailMessage.objects.filter(message_id=last_reference_id).first()

            if outgoing_msg:
                try:
                    thread = outgoing_msg.thread

                    # Flip email1/email2 so the mailbox account is now the receiver in the thread view
                    thread.email2 = recipient
                    thread.email1 = sender
                    fields_to_update = ['email1', 'email2']

                    # Optionally update subject if changed
                    if thread.subject != subject:
                        thread.subject = subject
                        fields_to_update.append('subject')

                    thread.save(update_fields=fields_to_update)

                    print("Reply matched to existing thread.")

                except Exception as e:
                    print(f"Error updating thread: {e}")
            else:
                print("No matching outgoing message found from In-Reply-To or References.")

            if not thread:
                similar_outgoing = OutgoingEmailMessage.objects.annotate(
                    similarity=TrigramSimilarity('subject', subject)
                ).filter(similarity__gt=0.6).order_by('-similarity').first()

                if similar_outgoing:
                    print("Found similar subject to our outgoing messages")
                    thread, _ = EmailThread.objects.get_or_create(
                        mailbox=mailbox,
                        email1=sender,
                        email2=recipient,
                        subject=subject
                    )
                else:
                    # Keyword matching
                    keywords = ['dispatch', 'service', 'load', 'driver', 'carrier', 'fmcsa', 'truck', 'trucking', 'quote', 'request']
                    if not any(keyword in subject.lower() for keyword in keywords):
                        Message.objects.get(id=msg.id).delete()
                        # to_delete_ids.append(msg.id)
                        deleted_count += 1
                        continue
                    else:
                        print("Found keyword")
                        thread, _ = EmailThread.objects.get_or_create(
                            mailbox=mailbox,
                            email1=sender,
                            email2=recipient,
                            subject=subject
                        )

            IncomingEmailMessage.objects.create(
                thread=thread,
                subject=subject,
                body=body,
                sender=sender,
                recipient=recipient,
                message_id=msg.message_id,
                in_reply_to=in_reply_to_header,
                received_at=received_at
            )
            new_messages_created = True  # Set the flag
            Message.objects.get(id=msg.id).delete()
            # to_delete_ids.append(msg.id)
            processed_count += 1

        except Exception as e:
            print(f"Error processing message {msg.id}: {e}")

    # if to_delete_ids:
    #     deleted_count += Message.objects.filter(id__in=to_delete_ids).delete()[0]

    print(f"Total processed: {processed_count}")
    print(f"Deleted {deleted_count} messages.")

    # Send notification only if new messages were created for this mailbox
    if new_messages_created:
        notification_thread = threading.Thread(target=send_unread_notification, args=(mailbox.from_email,))
        notification_thread.start()


def process_incoming_emails():
    """
    Main function to fetch and process incoming emails for all mailboxes.
    """
    # Step 1: Fetch new emails for all mailboxes

    troubled_mailboxes = []
    active_mailboxes = Mailbox.objects.all()
    print(f"Processing {active_mailboxes.count()} active mailboxes...")

    fetched_mailboxes = []

    for mailbox in active_mailboxes:
        print(f"Attempting to fetch mail for: ({mailbox.from_email})")
        try:
            mailbox.get_new_mail()
            print(f"Successfully fetched mail for: {mailbox.from_email}")
            fetched_mailboxes.append(mailbox)
        except imaplib.IMAP4.error as e:
            troubled_mailboxes.append(f"{mailbox.from_email} - Authentication Failed: {e}")
        except Exception as e:
            troubled_mailboxes.append(f"{mailbox.from_email} - Fetch Error: {e}")

    # Step 2: Process the successfully fetched emails using ThreadPoolExecutor
    if fetched_mailboxes:
        max_workers = min(5, len(fetched_mailboxes))
        print(f"Processing fetched mail for {len(fetched_mailboxes)} mailboxes")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for mailbox in fetched_mailboxes:
                executor.submit(process_mailbox, mailbox)
    else:
        print("No mailboxes had successful fetching attempts.")

    # Step 3: Send email report for troubled mailboxes
    if troubled_mailboxes and settings.EMAIL_HOST_USER:
        subject = "Problematic Email Accounts During Mail Fetch"
        body = "The following email accounts encountered issues while attempting to fetch new mail:\n\n"
        body += "\n".join(troubled_mailboxes)
        body += "\n\nPlease investigate these issues."
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, ["abdullahatif132@gmail.com",])
            print(f"Sent email report about {len(troubled_mailboxes)} problematic mailboxes to {settings.EMAIL_HOST_USER}")
        except Exception as e:
            print(f"Error sending email report: {e}")
    else:
        print("No troublesome mailboxes found during mail fetch.")




# dashboard.management.commands.process_incoming_emails.process_incoming_emails

# even if its a reply and my mailbox account should now be on the receiver side of the thread
# it won't now cz the thread is set so ...
# so if it's a reply, flip the recipient and sender, to shift the mailbox account on the reciver side
# incoming and outgoing message instances have the correct record of the sender and the  recipient, 
# I'm tweaking the thread only .... which only worries about the "participants" and not the sender/reciver logics .....
