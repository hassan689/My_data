from .models import EmailAccountAndLeads
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone
from django.conf import settings
from django.utils.encoding import force_str
import imaplib
import re
import random
import time


IMAP_SETTINGS_MAP = {
    'gmail':     {'host': 'imap.gmail.com', 'port': 993},
    'outlook':   {'host': 'outlook.office365.com', 'port': 993},
    'yahoo':     {'host': 'imap.mail.yahoo.com', 'port': 993},
    'zoho':      {'host': 'imap.zoho.com', 'port': 993},
    'hostinger': {'host': 'imap.hostinger.com', 'port': 993},
    'namecheap': {'host': 'imap.privateemail.com', 'port': 993},
    'godaddy':  {'host': 'imap.secureserver.net', 'port': 993},
    'titan':     {'host': 'imap.titan.email', 'port': 993},
}


def reschedule_or_finalize(campaign_id, account, template, next_lead_index, delay_seconds, use_batch=False, batch_size=10):
    """
    Decides whether to reschedule the next lead/batch or finalize the account.
    Includes a 'Hybrid' check (Cache + DB Fallback) to prevent premature failures.
    """
    from .tasks import finalize_drip_step_task, send_single_email, send_batch_emails
    
    # --- CASE 1: Account finished its leads ---
    if next_lead_index >= account.recipient_count:
        print(f"Account {account.id} finished its list for template {template.id}.")

        # 1. Mark this account as 'Ready' (Done) in the DB immediately
        if account.status != 'Stopped':
            account.status = 'Ready'
            account.save(update_fields=['status'])
        
        # 2. Check if we are the *last* account to finish
        total_key = f"drip_step_total_{template.id}"
        finished_key = f"drip_step_finished_{template.id}"
        
        trigger_finalizer = False
        
        try:
            # --- PRIMARY CHECK: CACHE (Fast) ---
            total_to_finish = cache.get(total_key)
            
            if total_to_finish is not None:
                # Cache is alive! Use it.
                current_finished = cache.incr(finished_key)
                if current_finished >= total_to_finish:
                    trigger_finalizer = True
            
            else:
                # --- SECONDARY CHECK: DATABASE (Reliable) ---
                # Cache is dead/expired. Don't panic. Check the DB.
                print(f"⚠️ Cache missing for Camp {campaign_id}. Checking DB for active workers...")
                
                # Are there any accounts still marked 'Processing'?
                others_still_running = EmailAccountAndLeads.objects.filter(
                    campaign_id=campaign_id,
                    status='Processing'
                ).exists()
                
                if not others_still_running:
                    print("DB confirms: No other accounts are Processing. We are the last.")
                    trigger_finalizer = True
                else:
                    print("DB reports: Other accounts are still running. Exiting.")

        except Exception as e:
            # If Cache crashes entirely, fallback to DB check
            print(f"Error checking cache: {e}. Fallback to DB.")
            others_still_running = EmailAccountAndLeads.objects.filter(
                campaign_id=campaign_id,
                status='Processing'
            ).exists()
            if not others_still_running:
                trigger_finalizer = True

        # 3. Trigger the Finalizer if we are the last one
        if trigger_finalizer:
            print(f"Finalizing step {template.step_number} for Campaign {campaign_id}.")
            finalize_drip_step_task.delay(campaign_id)
            
            # Cleanup (optional, harmless if keys are gone)
            cache.delete_many([total_key, finished_key])
        
        return # Done.

    # --- CASE 2: More leads to send (Reschedule) ---
    else:
        if use_batch:
            print(f"Rescheduling BATCH for {account.id} at lead {next_lead_index} in {delay_seconds}s.")
            send_batch_emails.apply_async(
                args=[campaign_id, account.id, template.id, next_lead_index, batch_size],
                countdown=delay_seconds
            )
        else:
            print(f"Rescheduling SINGLE for {account.id} for lead {next_lead_index} in {delay_seconds}s.")
            send_single_email.apply_async(
                args=[campaign_id, account.id, template.id, next_lead_index],
                countdown=delay_seconds
            )


def normalize_provider(provider_string):
    """
    Cleans the user-entered provider string to match a key
    in our IMAP_SETTINGS_MAP.
    """
    if not provider_string:
        return None
        
    provider_low = provider_string.lower()
    
    # Check for keywords
    if 'gmail' in provider_low or 'google' in provider_low:
        return 'gmail'
    if 'outlook' in provider_low or 'microsoft' in provider_low:
        return 'outlook'
    if 'yahoo' in provider_low:
        return 'yahoo'
    if 'zoho' in provider_low:
        return 'zoho'
    if 'hostinger' in provider_low:
        return 'hostinger'
    if 'namecheap' in provider_low or 'privateemail' in provider_low:
        return 'namecheap'
    if 'godaddy' in provider_low or 'secureserver' in provider_low:
        return 'godaddy'
    if 'titan' in provider_low:
        return 'titan'
        
    return None # We don't recognize it


def send_campaign_failure_alert(error_message, location, campaign_id=None):
    """
    Sends a critical alert email to the developer when a campaign fails.
    """
    subject = f"CRITICAL: Drip Campaign Failure in {location}"
    
    body = (
        f"An exception occurred in {location}.\n\n"
        f"Campaign ID: {campaign_id if campaign_id else 'Unknown'}\n"
        f"Error Message:\n{error_message}\n\n"
        f"Timestamp: {timezone.now()}\n"
        f"System Status: The campaign has been marked as 'Failed' in the database."
    )
    
    recipient_list = ["abdullahatif132@gmail.com"]
    from_email = getattr(settings, 'EMAIL_HOST_USER', getattr(settings, 'DEFAULT_FROM_EMAIL', None))

    try:
        print(f"⚠️ Sending Failure Alert to {recipient_list}...")
        send_mail(
            subject=subject,
            message=force_str(body),
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=False
        )
    except Exception as e:
        print(f"CRITICAL: Failed to send failure alert email! Original error: {error_message}. Email error: {e}")



def get_best_sent_folder(imap_conn):
    """
    Iterates through available folders to find the most likely 'Sent' folder.
    """
    try:
        # Get list of folders
        status, folders = imap_conn.list()
        if status != 'OK':
            return "Sent" # Fallback

        folders_list = []
        for f in folders:
            # Decode folder info (it comes as bytes)
            decoded = f.decode('utf-8', 'ignore')
            # Extract name (last part of the string usually in quotes)
            name_match = re.search(r'"([^"]+)"$', decoded) or re.search(r' (\S+)$', decoded)
            if name_match:
                folders_list.append(name_match.group(1))

        # Priority list for folder names
        candidates = ['Sent', 'Sent Items', 'Sent Mail', 'INBOX.Sent', 'INBOX.Sent Items']
        
        # 1. Exact match check
        for candidate in candidates:
            if candidate in folders_list:
                return candidate

        # 2. Case-insensitive check
        lower_folders = {f.lower(): f for f in folders_list}
        for candidate in candidates:
            if candidate.lower() in lower_folders:
                return lower_folders[candidate.lower()]

        return "Sent" # Default fallback
    except Exception as e:
        print(f"Error guessing sent folder: {e}")
        return "Sent"


def get_imap_connection(email_account):
    """
    Establishes and logs into an IMAP connection using database-stored settings.
    """
    # 1. Prioritize database fields
    imap_host = email_account.imap_host
    imap_port = email_account.imap_port or 993

    # 2. String-replacement fallback for legacy or "Other" accounts
    if not imap_host and email_account.host:
        if email_account.host.startswith('smtp.'):
            imap_host = email_account.host.replace('smtp.', 'imap.', 1)

    if not imap_host:
        return None

    # Retry 3 times with random 1-5s delay
    for attempt in range(3):
        try:
            imap_conn = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=15)
            decrypted_password = email_account.get_password()
            
            if not decrypted_password:
                return None

            imap_conn.login(email_account.email_address, decrypted_password)
            return imap_conn # Success
            
        except (imaplib.IMAP4.error, TimeoutError, OSError) as e:
            print(f"IMAP Attempt {attempt + 1} failed for {email_account.email_address}: {e}")
            try:
                if 'imap_conn' in locals():
                    imap_conn.logout()
            except Exception:
                pass  # Ignore logout errors
            if attempt < 2: # Don't sleep on the last attempt
                time.sleep(random.randint(1, 5))
    
    return None # Total failure after 3 attempts


def save_email_with_existing_connection(imap_conn, raw_email_message, message_id, cached_folder_name=None):
    """
    Uses an EXISTING open IMAP connection to append a message.
    """
    if not imap_conn:
        return

    try:
        # 1. Select Folder
        if cached_folder_name:
            sent_folder = cached_folder_name
        else:
            sent_folder = get_best_sent_folder(imap_conn)

        imap_conn.select(sent_folder)

        # 2. Check for existence
        if message_id:
            escaped_id = message_id.replace('\\', '\\\\').replace('"', '\\"')
            search_criteria = f'(HEADER Message-ID "{escaped_id}")'
            typ, data = imap_conn.search(None, search_criteria)
            if data and data[0]:
                return  # Already exists

        # 3. Append
        imap_conn.append(sent_folder, '\\Seen', None, raw_email_message)

    except Exception as e:
        print(f"Failed to append email {message_id}: {e}")
        # Note: If the connection actually broke, the caller needs to handle re-connecting
        raise e

