# Celery & Google
from celery import shared_task
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Django
from django.conf import settings
from django.utils import timezone
from django.contrib.postgres.search import TrigramSimilarity
import base64
import re
from datetime import datetime, timedelta, timezone as dt_timezone

# FOR FILE HANDLING
import io
import uuid
from django.core.files.base import ContentFile
from django.core.files import File

# Email handling
from email.header import decode_header
from email.utils import parseaddr
from email import message_from_bytes

# Project modules
from google_secrets import *
from dashboard.models import GmailToken
from unibox.models import EmailThread, OutgoingEmailMessage, IncomingEmailMessage, Attachment



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


class MessageWrapper:
    """
    A simple wrapper to make the Gmail API's raw message format
    compatible with the user's existing parsing logic.
    """
    def __init__(self, gmail_api_message):
        self._gmail_api_message = gmail_api_message
        self._email_object = None # Store the parsed email.message.Message

        # Decode raw message for initial parsing
        if 'raw' in self._gmail_api_message:
            msg_bytes = base64.urlsafe_b64decode(self._gmail_api_message['raw'])
            self._email_object = message_from_bytes(msg_bytes)
        elif 'payload' in self._gmail_api_message:
            # Fallback if 'raw' is not present, though 'RAW' format is best for email.message
            # For 'FULL' format, we'd iterate through parts to get content.
            # This path is less ideal for your existing email.message.Message expectations.
            pass # We'll rely on 'raw' for get_email_object

        # Pre-extract common fields
        self.message_id = self._email_object.get('Message-ID', self._gmail_api_message.get('id'))
        self.from_address = self._email_object.get('From')
        self.subject = self._email_object.get('Subject')
        # You might need to adjust based on how your `processed` field is set for incoming messages
        # For Gmail API messages, we use internalDate as received_at
        self.processed = datetime.fromtimestamp(
            int(self._gmail_api_message['internalDate']) / 1000,
            tz=dt_timezone.utc
        ) if 'internalDate' in self._gmail_api_message else timezone.now()

    def get_attachments(self):
        """
        Extracts attachment data from the email.message.Message object.
        Returns a list of dictionaries, each containing filename, mime_type, and content.
        """
        attachments = []
        if not self._email_object:
            return attachments

        for part in self._email_object.walk():
            # Check if it's an attachment and not the main body
            if part.get_filename() and not part.is_multipart():
                filename = part.get_filename()
                mime_type = part.get_content_type()
                
                # Decode the payload
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        attachments.append({
                            'filename': filename,
                            'mime_type': mime_type,
                            'content': payload, # Raw bytes of the attachment
                            'size': len(payload)
                        })
                except Exception as e:
                    print(f"Error decoding attachment payload for {filename}: {e}")
        return attachments


    def get_email_object(self):
        """Returns the email.message.Message object."""
        return self._email_object

    def __str__(self):
        return f"MessageWrapper(ID: {self.message_id}, Subject: {self.subject})"


def get_gmail_service(gmail_token_instance):
    """
    Authenticates and returns the Gmail API service object.
    Handles token refreshing if needed.
    """
    credentials = Credentials(
        token=gmail_token_instance.get_access_token(),
        refresh_token=gmail_token_instance.get_refresh_token(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=[GOOGLE_SCOPE]
    )

    # Check if the access token needs to be refreshed
    # 'credentials.expired' automatically checks expiry and presence of refresh token
    if credentials.expired and credentials.refresh_token:
        try:
            print(f"Attempting to refresh token for {gmail_token_instance.email_account.email_address}...")
            credentials.refresh(Request())
            # Update the database with the new token details
            gmail_token_instance.set_access_token(credentials.token)  # Use set_access_token()
            gmail_token_instance.expires_in = credentials.expires_in # Update this too
            # Refresh token *might* change after refresh, save it if it does
            if credentials.refresh_token:
                gmail_token_instance.set_refresh_token(credentials.refresh_token)
            gmail_token_instance.save()
            print(f"Token refreshed successfully for {gmail_token_instance.email_account.email_address}.")
        except Exception as e:
            print(f"Error refreshing token for {gmail_token_instance.email_account.email_address}: {e}")
            return None # Cannot proceed without a valid token

    try:
        service = build('gmail', 'v1', credentials=credentials)
        return service
    except HttpError as error:
        print(f"An API error occurred building service for {gmail_token_instance.email_account.email_address}: {error}")
        return None


def process_single_message(msg_wrapper, mailbox, recipient_email_address):
    """
    Processes a single MessageWrapper object to extract data and apply threading logic.
    """
    try:
        email_obj = msg_wrapper.get_email_object()
        if not email_obj:
            print(f"Skipping message {msg_wrapper.message_id}: Could not parse email object.")
            return False

        in_reply_to_header = email_obj.get('In-Reply-To')
        rfrncs_header = email_obj.get('References')

        subject_bytes, encoding = decode_header(email_obj.get('Subject', ''))[0]
        if isinstance(subject_bytes, bytes):
            subject = subject_bytes.decode(encoding or "utf-8", errors="replace")
        else:
            subject = subject_bytes
        subject = subject.strip()

        body = None
        if email_obj.is_multipart():
            for part in email_obj.walk():
                content_type = part.get_content_type()
                content_disposition = part.get("Content-Disposition", "")
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            body = extract_reply_only(decoded)
                            break
                    except Exception as e:
                        print(f"Error decoding part payload for {msg_wrapper.message_id}: {e}")
        else:
            try:
                payload = email_obj.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(email_obj.get_content_charset() or "utf-8", errors="replace")
                    body = extract_reply_only(decoded)
            except Exception as e:
                print(f"Error decoding main payload for {msg_wrapper.message_id}: {e}")

        if body is None:
            body = ""

        sender_tuple = parseaddr(msg_wrapper.from_address or '')
        sender = sender_tuple[1] or ''
        received_at = msg_wrapper.processed or timezone.now()

        recipient = recipient_email_address

        thread = None
        outgoing_msg = None

        outgoing_msg_instance = None 
        incoming_msg_instance = None

        # --- Self-loop / Outgoing Message Detection ---
        if sender.lower() == recipient.lower():
            print(f"Processing outgoing message {msg_wrapper.message_id} from {sender}.")

            # Try to find an existing thread where this mailbox is email1 (sender role)
            # OR create a new thread for this outgoing email
            thread, created = EmailThread.objects.get_or_create(
                mailbox=mailbox, # Owned by this mailbox
                email1=sender, # This mailbox is the sender in this thread's context
                email2=parseaddr(email_obj.get('To') or '')[1], # The actual recipient
                subject=subject,
                defaults={
                    'is_read': True # Outgoing messages are usually considered read
                }
            )
            if created:
                print(f"Created new thread {thread.id} for outgoing message {msg_wrapper.message_id}.")
            else:
                print(f"Found existing thread {thread.id} for outgoing message {msg_wrapper.message_id}.")

            outgoing_msg_instance = OutgoingEmailMessage.objects.create(
                thread=thread,
                subject=subject,
                body=body,
                recipient=parseaddr(email_obj.get('To') or '')[1], # Actual recipient of the outgoing email
                sender=sender, # Actual sender of the outgoing email (this mailbox)
                message_id=msg_wrapper.message_id,
                in_reply_to=in_reply_to_header,
                sent_at=received_at # Use received_at for timestamp consistency for sync
            )
            print(f"Saved outgoing message {msg_wrapper.message_id} to thread {thread.id}.")

            attachments_data = msg_wrapper.get_attachments()
            for attach_data in attachments_data:
                try:
                    # Generate a unique filename to prevent collisions
                    unique_filename = f"{uuid.uuid4()}_{attach_data['filename']}"
                    # Create a ContentFile from the raw bytes
                    django_file = ContentFile(attach_data['content'], name=unique_filename)

                    Attachment.objects.create(
                        outgoing_message=outgoing_msg_instance, # Link to the newly created outgoing message
                        file=django_file,
                        filename=attach_data['filename'],
                        mime_type=attach_data['mime_type'],
                        size=attach_data['size']
                    )
                    print(f"Saved attachment '{attach_data['filename']}' for outgoing message {msg_wrapper.message_id}.")
                except Exception as e:
                    print(f"Error saving attachment '{attach_data['filename']}' for outgoing message {msg_wrapper.message_id}: {e}")

            return True # Processed as an outgoing message

        # --- Handle Self-loop Duplicates (Incoming Side) ---
        try:
            # Check if this incoming message's ID matches an outgoing message's ID
            # This means it's a self-loop *already sent from our platform*
            outgoing_match = OutgoingEmailMessage.objects.get(message_id=msg_wrapper.message_id)
            print(f"Detected looped-back message {msg_wrapper.message_id} sent from another internal mailbox.")

            thread, created = EmailThread.objects.get_or_create(
                mailbox=mailbox,
                email1=sender,
                email2=recipient,
                subject=subject,
                defaults={
                    'is_read': False
                }
            )
            if created:
                print(f"Created new thread for self-loop (incoming side): {thread.id}")
            else:
                print(f"Found existing thread for self-loop (incoming side): {thread.id}")

            incoming_msg_instance = IncomingEmailMessage.objects.create( 
                thread=thread,
                subject=subject,
                body=body,
                sender=sender,
                recipient=recipient,
                message_id=msg_wrapper.message_id,
                in_reply_to=in_reply_to_header,
                received_at=received_at
            )
            print(f"Saved incoming self-loop message {msg_wrapper.message_id} to thread {thread.id}.")
            
            attachments_data = msg_wrapper.get_attachments()
            for attach_data in attachments_data:
                try:
                    unique_filename = f"{uuid.uuid4()}_{attach_data['filename']}"
                    django_file = ContentFile(attach_data['content'], name=unique_filename)

                    Attachment.objects.create(  
                        incoming_message=incoming_msg_instance,
                        file=django_file,
                        filename=attach_data['filename'],
                        mime_type=attach_data['mime_type'],
                        size=attach_data['size']
                    )
                    print(f"Saved attachment '{attach_data['filename']}' for incoming self-loop message {msg_wrapper.message_id}.")
                except Exception as e:
                    print(f"Error saving attachment '{attach_data['filename']}' for incoming self-loop message {msg_wrapper.message_id}: {e}")
            
            return True

        except OutgoingEmailMessage.DoesNotExist:
            pass # Not a looped-back message from our own platform, proceed normally with other threading logic


        # --- Standard Incoming Message Processing ---

        # Try to find the matching outgoing message using In-Reply-To
        if in_reply_to_header:
            outgoing_msg = OutgoingEmailMessage.objects.filter(message_id=in_reply_to_header).first()

        # Fallback: try using the last message_id from References header
        if not outgoing_msg and rfrncs_header:
            message_ids = rfrncs_header.strip().split()
            if message_ids:
                last_reference_id = message_ids[-1]
                outgoing_msg = OutgoingEmailMessage.objects.filter(message_id=last_reference_id).first()

        if outgoing_msg:
            try:
                thread = outgoing_msg.thread
                # Ensure the thread's email1/email2 reflect the actual sender/recipient for this incoming message
                # relative to the mailbox owner (email2)
                thread.email1 = sender
                thread.email2 = recipient
                fields_to_update = ['email1', 'email2']

                if thread.subject != subject:
                    thread.subject = subject
                    fields_to_update.append('subject')

                thread.save(update_fields=fields_to_update)
                print(f"Reply {msg_wrapper.message_id} matched to existing thread {thread.id}.")

            except Exception as e:
                print(f"Error updating thread for reply {msg_wrapper.message_id}: {e}")
        else:
            print(f"No matching outgoing message found from In-Reply-To or References for {msg_wrapper.message_id}.")

        if not thread:
            # Try to find similar outgoing message subjects for new thread creation
            similar_outgoing = OutgoingEmailMessage.objects.annotate(
                similarity=TrigramSimilarity('subject', subject)
            ).filter(similarity__gt=0.6, sender=recipient).order_by('-similarity').first()

            if similar_outgoing:
                print(f"Found similar subject for {msg_wrapper.message_id} to our outgoing messages.")
                thread, _ = EmailThread.objects.get_or_create(
                    mailbox=mailbox,
                    email1=sender,
                    email2=recipient,
                    subject=subject
                )
            else:
                keywords = ['dispatch', 'service', 'load', 'driver', 'carrier', 'fmcsa', 'truck', 'trucking', 'quote', 'request']
                if not any(keyword in subject.lower() for keyword in keywords):
                    print(f"Message {msg_wrapper.message_id} subject '{subject}' not relevant. Ignoring.")
                    return False
                else:
                    print(f"Found relevant keyword in subject for {msg_wrapper.message_id}.")
                    thread, _ = EmailThread.objects.get_or_create(
                        mailbox=mailbox,
                        email1=sender,
                        email2=recipient,
                        subject=subject
                    )

        if thread:
            incoming_msg_instance = IncomingEmailMessage.objects.create(
                thread=thread,
                subject=subject,
                body=body,
                sender=sender,
                recipient=recipient,
                message_id=msg_wrapper.message_id,
                in_reply_to=in_reply_to_header,
                received_at=received_at
            )
            print(f"Saved incoming message {msg_wrapper.message_id} to thread {thread.id}.")
            
            attachments_data = msg_wrapper.get_attachments()
            for attach_data in attachments_data:
                try:
                    unique_filename = f"{uuid.uuid4()}_{attach_data['filename']}"
                    django_file = ContentFile(attach_data['content'], name=unique_filename)

                    Attachment.objects.create(
                        incoming_message=incoming_msg_instance,
                        file=django_file,
                        filename=attach_data['filename'],
                        mime_type=attach_data['mime_type'],
                        size=attach_data['size']
                    )
                    print(f"Saved attachment '{attach_data['filename']}' for incoming message {msg_wrapper.message_id}.")
                except Exception as e:
                    print(f"Error saving attachment '{attach_data['filename']}' for incoming message {msg_wrapper.message_id}: {e}")
            
            return True
        else:
            print(f"Could not find or create a thread for message {msg_wrapper.message_id}. Skipping.")
            return False

    except Exception as e:
        print(f"An unexpected error occurred processing message {msg_wrapper.message_id}: {e}")
        return False


@shared_task(bind=True, default_retry_delay=300, max_retries=5)
def fetch_gmail_messages_for_all_accounts(self):
    """
    Celery task to fetch and process new Gmail messages for all connected accounts.
    """
    gmail_tokens = GmailToken.objects.all()
    print(f"\\n Starting Gmail message sync for {len(gmail_tokens)} accounts... \\n")

    for gmail_token_instance in gmail_tokens:
        user_email_address = gmail_token_instance.email_account.email_address
        print(f"Processing account: {user_email_address}")

        service = get_gmail_service(gmail_token_instance)
        if not service:
            print(f"Skipping {user_email_address} due to authentication issues.")
            continue

        try:
            user_id = 'me'
            new_last_history_id = None

            if not gmail_token_instance.last_history_id:
                print(f"Performing initial sync (last 30 days) for {user_email_address}...")
                thirty_days_ago = (timezone.now() - timedelta(days=30)).strftime('%Y/%m/%d')
                query = f"after:{thirty_days_ago}"

                messages_list_response = service.users().messages().list(
                    userId=user_id,
                    q=query,
                    maxResults=50
                ).execute()

                messages = messages_list_response.get('messages', [])
                print(f"Found {len(messages)} messages for initial sync for {user_email_address}.")

                for msg_item in messages:
                    try:
                        full_message_data = service.users().messages().get(
                            userId=user_id,
                            id=msg_item['id'],
                            format='raw'
                        ).execute()
                        msg_wrapper = MessageWrapper(full_message_data)
                        process_single_message(msg_wrapper, gmail_token_instance, user_email_address)
                    except HttpError as msg_error:
                        print(f"Error fetching initial message {msg_item['id']}: {msg_error}")
                    except Exception as e:
                        print(f"Error processing initial message {msg_item['id']}: {e}")

                profile = service.users().getProfile(userId=user_id).execute()
                new_last_history_id = profile['historyId']
                print(f"Initial sync complete. Set historyId for {user_email_address} to {new_last_history_id}.")

            else:
                print(f"Performing incremental sync from historyId {gmail_token_instance.last_history_id} for {user_email_address}...")
                history_response = service.users().history().list(
                    userId=user_id,
                    startHistoryId=gmail_token_instance.last_history_id,
                    historyTypes=['messageAdded', 'messageDeleted', 'labelAdded', 'labelRemoved'] # Added 'messagesDeleted'
                ).execute()

                histories = history_response.get('history', [])
                print(f"Found {len(histories)} history records for {user_email_address}.")

                for history in histories:
                    # Process New Messages
                    if 'messagesAdded' in history:
                        for msg_added in history['messagesAdded']:
                            message_id = msg_added['message']['id']
                            try:
                                full_message_data = service.users().messages().get(
                                    userId=user_id,
                                    id=message_id,
                                    format='raw'
                                ).execute()
                                msg_wrapper = MessageWrapper(full_message_data)
                                process_single_message(msg_wrapper, gmail_token_instance, user_email_address)
                            except HttpError as msg_error:
                                print(f"Error fetching history message {message_id}: {msg_error}")
                            except Exception as e:
                                print(f"Error processing history message {message_id}: {e}")

                    # Process Deleted Messages
                    if 'messagesDeleted' in history:
                        for msg_deleted in history['messagesDeleted']:
                            message_id = msg_deleted['message']['id']
                            print(f"Detected deletion for message ID: {message_id}")
                            try:
                                incoming_msg = IncomingEmailMessage.objects.filter(message_id=message_id, recipient=user_email_address).first()
                                if incoming_msg:
                                    thread_to_check = incoming_msg.thread
                                    incoming_msg.delete()
                                    print(f"Deleted IncomingEmailMessage {message_id} from DB.")

                                    # Check if the thread is now empty
                                    total_messages_in_thread = thread_to_check.incoming_messages.count() + thread_to_check.outgoing_messages.count()
                                    if total_messages_in_thread == 0:
                                        thread_to_check.delete()
                                        print(f"Deleted empty EmailThread {thread_to_check.id} from DB.")
                                else:
                                    # Also check OutgoingEmailMessage, if you decide to track all sent emails
                                    # For now, it only tracks those sent *from* the platform.
                                    outgoing_msg = OutgoingEmailMessage.objects.filter(message_id=message_id, sender=user_email_address).first()
                                    if outgoing_msg:
                                        thread_to_check = outgoing_msg.thread
                                        outgoing_msg.delete()
                                        print(f"Deleted OutgoingEmailMessage {message_id} from DB.")
                                        total_messages_in_thread = thread_to_check.incoming_messages.count() + thread_to_check.outgoing_messages.count()
                                        if total_messages_in_thread == 0:
                                            thread_to_check.delete()
                                            print(f"Deleted empty EmailThread {thread_to_check.id} from DB.")
                                    else:
                                        print(f"Message {message_id} not found in DB for deletion.")

                            except Exception as e:
                                print(f"Error handling deletion for message {message_id}: {e}")


                    # Process Label Changes (e.g., read/unread status)
                    if 'labelsAdded' in history:
                        for label_change in history['labelsAdded']:
                            message_id = label_change['message']['id']
                            labels = label_change.get('labelIds', [])
                            if 'UNREAD' in labels:
                                IncomingEmailMessage.objects.filter(message_id=message_id, recipient=user_email_address).update(is_read=False)
                                print(f"Message {message_id} marked as UNREAD.")
                    if 'labelsRemoved' in history:
                        for label_change in history['labelsRemoved']:
                            message_id = label_change['message']['id']
                            labels = label_change.get('labelIds', [])
                            if 'UNREAD' not in labels:
                                incoming_msg_qs = IncomingEmailMessage.objects.filter(message_id=message_id, recipient=user_email_address)
                                if incoming_msg_qs.exists():
                                    incoming_msg_qs.update(is_read=True)
                                    print(f"Message {message_id} marked as READ.")
                                    try:
                                        thread = incoming_msg_qs.first().thread
                                        # Only mark thread as read if ALL its incoming messages are read
                                        if not thread.incoming_messages.filter(is_read=False).exists():
                                            thread.is_read = True
                                            thread.save(update_fields=['is_read'])
                                            print(f"Thread {thread.id} marked as READ.")
                                    except Exception as e:
                                        print(f"Error updating thread read status for message {message_id}: {e}")


                if 'historyId' in history_response:
                    new_last_history_id = history_response['historyId']
                elif histories:
                    new_last_history_id = histories[-1]['id']

            if new_last_history_id and new_last_history_id != gmail_token_instance.last_history_id:
                gmail_token_instance.last_history_id = new_last_history_id
                gmail_token_instance.save(update_fields=['last_history_id'])
                print(f"Updated final historyId for {user_email_address} to {new_last_history_id}.")

        except HttpError as error:
            print(f"An API error occurred during sync for {user_email_address}: {error}")
            self.retry(exc=error)
        except Exception as e:
            print(f"An unexpected error occurred during sync for {user_email_address}: {e}")
            self.retry(exc=e)

    print("Gmail message sync task finished.")


