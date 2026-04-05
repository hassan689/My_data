import random
import re
import smtplib
import imaplib
import email
import time
from email.utils import parseaddr
from .models import WarmupProfile
from django.core.mail import get_connection
from django.db.models import Count, Q, Min
from django.db import transaction
from django.utils import timezone
from users.models import EmailAccount

############################ Pipeline 1: Outbound Warmup Emails ############################

# Retained your spintax logic and templates, combined into a random pool
WARMUP_TEMPLATES = [
    """{Hi|Hello|Hey} {recipient_name}, {quick question --|just curious,} {do you have any recommendations for tools for logistics?|Are you open to a quick chat?} {Best,|Thanks,} {sender_name}""",
    """{Thanks for connecting.|Appreciate the time.} {Mainly|Mostly} focused on {how you're handling shipping|the shift in market demand}. {Is automation the big focus this year?|Thoughts?} {Best,} {sender_name}""",
    """{I heard|Read somewhere} that better visibility is {making a huge difference|the big focus}. {Actually,|By the way,} have you tried any specific platforms? {Cheers,} {sender_name}""",
    """{If you're free|If you have time} {later this week|next week}, {maybe we could|perhaps we can} {sync|connect} for 10 minutes? {Let me know,|Talk soon,} {sender_name}"""
]

WARMUP_SUBJECT_TEMPLATE = "{For {recipient_name}|{recipient_name} - {sender_company}|{recipient_company} / {sender_company}|{recipient_name} @ {recipient_company}|{sender_company} + {recipient_company}|{recipient_company} x {sender_company}|For the {recipient_company} team|{recipient_name}|{recipient_name} - following up|Touching base|Checking in|Hello {recipient_name}|Hi {recipient_name}|Hey {recipient_name}|{recipient_name} - hi|Hi from {sender_company}|From {sender_company}|Small note|Just a note|A quick one|Small update|{recipient_name} - check this out|Just saw this|Saw this and thought of you|Noticed this|Check this|Regarding {recipient_company}|About {recipient_company}|News for {recipient_company}|{recipient_company} news|Update for {recipient_name}|{recipient_name} at {recipient_company}|Found you through {recipient_company}|Saw your profile|Thinking about {recipient_company}|Thinking of you|Idea for {recipient_company}|Connection|Chat|Meeting|{sender_company} / {recipient_name}|Regarding {recipient_name}|{recipient_name} - update|Saw your work|Found you|{recipient_name} - saw your latest|Hi from the team|From the {sender_company} team|Hello!|Hi there}"

def spin_text(text):
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = pattern.search(text)
        if not match: break
        full_match, content = match.group(0), match.group(1)
        text = text.replace(full_match, random.choice(content.split('|')), 1)
    return text

def generate_fresh_spintax(recipient_first_name, sender_company_name, recipient_company_name=None):
    # Generates a random body and subject
    raw_body = random.choice(WARMUP_TEMPLATES)
    r_name = recipient_first_name.title() if recipient_first_name else "there"
    s_company = sender_company_name if sender_company_name else "our company"
    r_company = recipient_company_name if recipient_company_name else "your company"

    raw_body = raw_body.replace("{recipient_name}", r_name).replace("{sender_name}", s_company)
    body = spin_text(raw_body)

    # raw_subj = WARMUP_SUBJECT_TEMPLATE.replace("{recipient_name}", r_name).replace("{sender_company}", s_company)
    raw_subj = WARMUP_SUBJECT_TEMPLATE.replace("{recipient_name}", r_name).replace("{sender_company}", s_company).replace("{recipient_company}", r_company)
    subject = spin_text(raw_subj)
    
    return subject, body


# for the reply body
def generate_spintax_body(recipient_first_name, sender_company_name):
    """
    Generates a coherent, unique email body using Spintax.
    """
    # 1. Get the master template
    raw_text = random.choice(WARMUP_TEMPLATES)
    
    # 2. Inject Variables BEFORE spinning (so they don't break the syntax)
    # We use explicit replace or f-string logic
    raw_text = raw_text.replace("{recipient_name}", recipient_first_name or "there")
    raw_text = raw_text.replace("{sender_company}", sender_company_name or "Company")

    # 3. Spin it
    final_body = spin_text(raw_text)
    
    return final_body


def get_email_connection(email_account, decrypted_password):
    """
    Establishes and opens an SMTP connection for sending emails.
    """
    use_tls = email_account.server_type == "STARTTLS" or email_account.server_type == "TLS"
    use_ssl = email_account.server_type == "SSL"

    try:
        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=email_account.host,
            port=email_account.port_number,
            username=email_account.email_address,
            password=decrypted_password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=30,  # lower global timeout
        )
        connection.open()
        return connection
    except (TimeoutError, OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as e:
        print(f"[SMTP Timeout] Could not connect to {email_account.email_address}: {e}")
        return None

def get_water_level_target(sender_account):
    """
    Finds the active account with the LEAST amount of received emails today,
    excluding accounts belonging to the sender's user.
    """
    today = timezone.now().date()
    
    candidates = EmailAccount.objects.filter(
        warmup_profile__status='Warming',
        warmup_profile__warmup_enabled=True,
        black_list=False
    ).exclude(user_id=sender_account.user_id).annotate(
        received_today=Count(
            'received_pool_emails', 
            filter=Q(received_pool_emails__sent_at__date=today)
        )
    )

    if not candidates.exists():
        return None

    # Find the lowest receive count
    min_receives = candidates.aggregate(Min('received_today'))['received_today__min']
    
    # Filter to only the lowest and pick one randomly
    priority_targets = candidates.filter(received_today=min_receives)
    return random.choice(list(priority_targets))


################################# Pipeline 2: IMAP and Replies #################################

def get_warmup_imap_connection(email_account, retries=2):
    """Establishes IMAP connection with timeout protection and retry."""
    for attempt in range(retries):
        try:
            imap_host = email_account.imap_host
            imap_port = email_account.imap_port or 993
            
            imap_conn = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=30)
            decrypted_password = email_account.get_password()
            
            if not decrypted_password:
                return None
                
            imap_conn.login(email_account.email_address, decrypted_password)
            return imap_conn
        
        except Exception as e:
            print(f"IMAP attempt {attempt+1} failed for {email_account.email_address}: {e}")
            if attempt == retries - 1:
                print(f"IMAP failed after {retries} attempts for {email_account.email_address}")
                return None
            time.sleep(5)  # Wait before retry
    return None


def extract_text_body(msg):
    """Safely extracts the plain text body from an email payload."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors='replace')
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors='replace')
    return body


def process_single_inbox(imap_conn):
    """
    Scans INBOX for unread warmup emails using the custom header.
    Returns a list of dictionaries containing email data for replies.
    """
    found_emails = []
    try:
        status, _ = imap_conn.select('"INBOX"', readonly=False)
        if status != 'OK': return found_emails

        # The Anchor: Strict header search for UNSEEN warmup emails
        status, data = imap_conn.uid('search', None, '(UNSEEN HEADER X-Warmup-ID "")')
        if status == 'OK' and data[0]:
            uids = data[0].split()
            
            for target_uid in uids:
                status, msg_data = imap_conn.uid('fetch', target_uid, "(RFC822)")
                if status == 'OK' and msg_data and msg_data[0]:
                    raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                    if raw_email:
                        msg = email.message_from_bytes(raw_email)

                        # Only process if it has our X-Warmup-ID header
                        if 'X-Warmup-ID' not in msg:
                            # Not our email - mark as seen and skip
                            imap_conn.uid('store', target_uid, '+FLAGS', '\\Seen')
                            continue
                        
                        # Extract data for the reply worker
                        message_id = msg.get('Message-ID', '')
                        subject = msg.get('Subject', '')
                        from_header = msg.get('From', '')
                        _, from_email_address = parseaddr(from_header)
                        
                        # We only need enough body to quote it in the reply
                        body = extract_text_body(msg) 
                        
                        if message_id and from_email_address:
                            found_emails.append({
                                'message_id': message_id,
                                'subject': subject,
                                'from_email': from_email_address,
                                'body': body[:500] # Truncate to save memory
                            })
                            
                # Mark as seen
                imap_conn.uid('store', target_uid, '+FLAGS', '\\Seen')
                
    except Exception as e:
        print(f"IMAP Inbox Processing Error: {e}")
        
    return found_emails


def get_spam_folder(imap_conn, email_address=None):
    """Dynamically locates the Spam/Junk folder using attributes or common names."""
    try:
        status, folder_list = imap_conn.list()
        if status != 'OK':
            return None
        
        # Special handling for Gmail
        if email_address and '@gmail.com' in email_address:
            # Try common Gmail spam folder paths
            gmail_spam_folders = ['[Gmail]/Spam', '[Gmail]/Junk', 'Spam', 'Junk']
            for folder in gmail_spam_folders:
                try:
                    # Test if folder exists by trying to select it
                    test_status, _ = imap_conn.select(f'"{folder}"', readonly=True)
                    if test_status == 'OK':
                        return folder
                except:
                    continue
        
        # Parse folder list
        folders = []
        for f_info in folder_list:
            if isinstance(f_info, bytes):
                f_str = f_info.decode('utf-8', errors='ignore')
            else:
                f_str = str(f_info)
            
            # Parse IMAP folder listing format
            # Format: (flags) "delimiter" "folder_name"
            match = re.match(r'\(([^)]+)\)\s+"([^"]*)"\s+"?(.+)"?', f_str)
            if not match:
                # Try alternative parsing
                parts = f_str.split('"')
                if len(parts) >= 3:
                    flags = parts[0].strip('() ')
                    delimiter = parts[1] if len(parts) > 1 else ''
                    folder_name = parts[2] if len(parts) > 2 else ''
                    folders.append({
                        'flags': flags,
                        'delimiter': delimiter,
                        'name': folder_name
                    })
                continue
                
            flags, delimiter, folder_name = match.groups()
            folder_name = folder_name.strip('"')
            folders.append({
                'flags': flags,
                'delimiter': delimiter,
                'name': folder_name
            })
        
        # Priority 1: Check for Junk flag
        for folder in folders:
            if '\\Junk' in folder['flags']:
                return folder['name']
        
        # Priority 2: Check for Spam flag
        for folder in folders:
            if '\\Spam' in folder['flags']:
                return folder['name']
        
        # Priority 3: Match by common names (case insensitive)
        spam_names = [
            'spam', 'junk', 'bulk', 'bulk mail', 'junk mail', 'junk e-mail',
            'unsolicited', 'trash', 'spam folder', 'junk folder'
        ]
        
        # Also include Gmail variants
        gmail_variants = ['[gmail]/spam', '[gmail]/junk']
        
        all_spam_names = spam_names + gmail_variants
        
        for folder in folders:
            folder_lower = folder['name'].lower()
            for spam_name in all_spam_names:
                if spam_name in folder_lower or folder_lower == spam_name:
                    return folder['name']
        
        # Priority 4: Check for localized spam folder names
        # Some providers use translated names
        localized_names = ['correo no deseado', 'pourriel', 'unzustellbar']
        for folder in folders:
            folder_lower = folder['name'].lower()
            for local_name in localized_names:
                if local_name in folder_lower:
                    return folder['name']
        
        # Priority 5: Last resort - check if INBOX has a spam-like flag
        for folder in folders:
            if folder['name'].upper() == 'INBOX':
                continue
            # If nothing else found, return any folder that isn't INBOX, Sent, Drafts, Trash
            # This is risky but better than nothing
            excluded = ['inbox', 'sent', 'drafts', 'trash', 'archive', 'sent mail', 'sent items']
            if folder['name'].lower() not in excluded:
                # Could be spam folder
                pass
        
        return None
        
    except Exception as e:
        print(f"Error finding spam folder: {e}")
        return None


def rescue_from_spam(imap_conn, email_address):
    rescued_count = 0
    try:
        spam_folder = get_spam_folder(imap_conn, email_address)
        if not spam_folder: 
            return 0

        status, _ = imap_conn.select(f'"{spam_folder}"', readonly=False)
        if status == 'OK':
            # Use the same search as process_single_inbox
            status, data = imap_conn.uid('search', None, '(UNSEEN HEADER X-Warmup-ID "")')
            if status == 'OK' and data[0]:
                uids = data[0].split()
                
                for target_uid in uids:
                    # Verify it's our email (safety check)
                    status, msg_data = imap_conn.uid('fetch', target_uid, "(RFC822)")
                    if status == 'OK' and msg_data and msg_data[0]:
                        raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if raw_email:
                            msg = email.message_from_bytes(raw_email)
                            
                            # Only process if it has our X-Warmup-ID header
                            if 'X-Warmup-ID' in msg:
                                rescued_count += 1
                                # Copy to INBOX (don't try to modify flags that may not exist)
                                copy_status = imap_conn.uid('copy', target_uid, '"INBOX"')
                                
                                if copy_status[0] == 'OK':
                                    # Mark original for deletion
                                    imap_conn.uid('store', target_uid, '+FLAGS', '\\Deleted')
                    else:
                        # Still mark as seen even if not our email
                        imap_conn.uid('store', target_uid, '+FLAGS', '\\Seen')
                        
                # Expunge deleted messages
                imap_conn.expunge()
    except Exception as e:
        print(f"IMAP Rescue Error: {e}")
        
    return rescued_count


def process_audit_results(results_map):
    """
    Parses API results and bulk updates accounts marked as 'undeliverable'.
    """
    emails = list(results_map.keys())
    
    # Fetch all accounts and their profiles in one JOIN query
    accounts = EmailAccount.objects.select_related('warmup_profile').filter(
        email_address__in=emails
    )

    accounts_to_update = []
    profiles_to_update = []

    for account in accounts:
        data = results_map.get(account.email_address, {})
        status = str(data.get('status', '')).lower()
        
        if status == 'undeliverable' and not account.black_list:
            # Update EmailAccount instance
            account.black_list = True
            account.is_warmup_target = False
            accounts_to_update.append(account)

            # Update related WarmupProfile instance
            profile = getattr(account, 'warmup_profile', None)
            if profile:
                profile.status = 'Error'
                profile.warmup_enabled = False
                profiles_to_update.append(profile)

    if not accounts_to_update:
        return

    # Atomic block to ensure both tables are updated or neither
    try:
        with transaction.atomic():
            if accounts_to_update:
                EmailAccount.objects.bulk_update(
                    accounts_to_update, 
                    ['black_list', 'is_warmup_target']
                )
            
            if profiles_to_update:
                WarmupProfile.objects.bulk_update(
                    profiles_to_update, 
                    ['status', 'warmup_enabled']
                )
                
        print(f"Audit Complete: Blacklisted {len(accounts_to_update)} accounts.")
    except Exception as e:
        print(f"Failed to batch update audit results: {e}")

