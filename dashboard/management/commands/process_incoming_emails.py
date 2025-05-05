from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor
from django_mailbox.models import Mailbox, Message
from dashboard.models import IncomingEmailMessage, OutgoingEmailMessage
from unibox.models import EmailThread
from django.contrib.postgres.search import TrigramSimilarity
from users.models import EmailAccount
from email.utils import parseaddr


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

                subject = msg.subject or ''
                body = msg.body or ''
                sender_tuple = parseaddr(msg.from_address or '')
                sender = sender_tuple[1] or ''
                received_at = msg.processed or timezone.now()

                thread = None

                if in_reply_to_header:
                    try:
                        outgoing_msg = OutgoingEmailMessage.objects.get(message_id=in_reply_to_header)
                        thread = outgoing_msg.thread
                        print("Reply to campaign message")
                    except Exception as e:
                        print(f"Error finding outgoing message: {e}")

                if not thread:
                    similar_outgoing = OutgoingEmailMessage.objects.annotate(
                        similarity=TrigramSimilarity('subject', subject)
                    ).filter(similarity__gt=0.6).order_by('-similarity').first()

                    if similar_outgoing:
                        print("Found similar subject to our outgoing messages")
                        thread = EmailThread.objects.create(
                            email_account=similar_outgoing.email_account,
                            subject=subject
                        )
                    else:
                        keywords = ['dispatch', 'service', 'load', 'driver', 'carrier', 'fmcsa', 'truck', 'quote', 'request']
                        if not any(keyword in subject.lower() for keyword in keywords):
                            to_delete_ids.append(msg.id)
                            deleted_count += 1
                            continue
                        else:
                            print("Found keyword")
                            email_account = EmailAccount.objects.get(email_address=mailbox.from_email)
                            thread = EmailThread.objects.create(
                                email_account=email_account,
                                subject=subject
                            )

                IncomingEmailMessage.objects.create(
                    email_account=thread.email_account,
                    thread=thread,
                    subject=subject,
                    body=body,
                    sender=sender,
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
