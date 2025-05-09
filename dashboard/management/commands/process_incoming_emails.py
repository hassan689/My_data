from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor
from django_mailbox.models import Mailbox, Message
from dashboard.models import IncomingEmailMessage, OutgoingEmailMessage
from unibox.models import EmailThread
from django.contrib.postgres.search import TrigramSimilarity
from email.utils import parseaddr
from email import message_from_bytes
import base64
import re


def extract_reply_only(body):
        # Common patterns that mark start of quoted content
        quote_patterns = [
            r"On\s.+?wrote:",      # Matches "On [date] wrote:"
            r"From:\s.+",          # Matches "From: <email>"
            r"Sent:\s.+",          # Matches "Sent: <date>"
            r">",                  # Matches quoted lines
        ]
        
        # Combine patterns into one regex
        combined_pattern = re.compile("|".join(quote_patterns), re.IGNORECASE)
        
        match = combined_pattern.search(body)
        if match:
            return body[:match.start()].strip()
        
        return body.strip()

class Command(BaseCommand):
    help = 'Process incoming emails and organize them into threads.'

    def handle(self, *args, **options):
        # Step 1: Fetch new emails for all mailboxes
        call_command('getmail')

        # Step 2: Fetch all registered mailboxes
        mailboxes = Mailbox.objects.all()
        print(mailboxes)

        # Limit processing to 5 mailboxes at a time (or less if fewer exist)
        max_workers = min(5, mailboxes.count())

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for mailbox in mailboxes:
                executor.submit(self.process_mailbox, mailbox)

    def process_mailbox(self, mailbox):
        
        print(f"called for {mailbox}")

        # Step 3: Process only messages related to this mailbox
        messages = Message.objects.filter(mailbox=mailbox)
        print(messages)
        to_delete_ids = []  # Track IDs of messages to delete later
        processed_count = 0  # Count of processed messages
        deleted_count = 0  # Count of deleted messages

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
                        sender=recipient,      # Flip
                        recipient=sender,      # Flip
                        message_id=msg.message_id,
                        in_reply_to=in_reply_to_header,
                        received_at=received_at
                    )

                    to_delete_ids.append(msg.id)
                    processed_count += 1
                    continue  # Skip rest of logic for this case
                except OutgoingEmailMessage.DoesNotExist:
                    pass  # Not a looped-back message, proceed normally

                # even if its a reply and my mailbox account should now be on the receiver side of the thread
                # it won't now cz the thread is set so ...
                # so if it's a reply, flip the recipient and sender, to shift the mailbox account on the reciver side
                # incoming and outgoing message instances have the correct record of the sender and the  recipient, 
                # I'm tweaking the thread only .... which only worries about the "participants" and not the sender/reciver logics .....


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

                      # email2=recipient 
                      # This is bcz the system is incoming msg focused and our campaign sending account is 
                      # supposed / expected to be on the recip. side of the thread

                    # Keyword matching
                    else:
                        keywords = ['dispatch', 'service', 'load', 'driver', 'carrier', 'fmcsa', 'truck', 'quote', 'request']
                        if not any(keyword in subject.lower() for keyword in keywords):
                            to_delete_ids.append(msg.id)
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

                to_delete_ids.append(msg.id)
                processed_count += 1

            except Exception as e:
                print(f"Error processing message {msg.id}: {e}")


        if to_delete_ids:
            deleted_count += Message.objects.filter(id__in=to_delete_ids).delete()[0]

        print(f"Total processed: {processed_count}")
        print(f"Deleted {deleted_count} messages.")
