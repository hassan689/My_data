import random
import re
import smtplib
import imaplib
import email
from email.utils import parseaddr
from .models import WarmupEmail
from django.core.mail import get_connection
from django.db.models import Count, Q, Min
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

WARMUP_SUBJECT_TEMPLATE = "{Quick note for {recipient_name}|{Hello|Hi} {recipient_name}|Checking in|Thoughts on operations|Reaching out from {sender_company}|Quick chat sometime this week?}"

def spin_text(text):
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = pattern.search(text)
        if not match: break
        full_match, content = match.group(0), match.group(1)
        text = text.replace(full_match, random.choice(content.split('|')), 1)
    return text

def generate_fresh_spintax(recipient_first_name, sender_company_name):
    # Generates a random body and subject
    raw_body = random.choice(WARMUP_TEMPLATES)
    r_name = recipient_first_name.title() if recipient_first_name else "there"
    s_company = sender_company_name if sender_company_name else "our company"

    raw_body = raw_body.replace("{recipient_name}", r_name).replace("{sender_name}", s_company)
    body = spin_text(raw_body)

    raw_subj = WARMUP_SUBJECT_TEMPLATE.replace("{recipient_name}", r_name).replace("{sender_company}", s_company)
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

def get_warmup_imap_connection(email_account):
    """Establishes IMAP connection with timeout protection."""
    try:
        imap_host = email_account.imap_host
        imap_port = email_account.imap_port or 993
        
        imap_conn = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=15)
        decrypted_password = email_account.get_password()
        
        if not decrypted_password: return None
            
        imap_conn.login(email_account.email_address, decrypted_password)
        return imap_conn
    except Exception as e:
        print(f"IMAP Error [{email_account.email_address}]: {e}")
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
        status, data = imap_conn.uid('search', None, '(UNSEEN HEADER "X-Warmup-ID" "")')
        if status == 'OK' and data[0]:
            uids = data[0].split()
            
            for target_uid in uids:
                status, msg_data = imap_conn.uid('fetch', target_uid, "(RFC822)")
                if status == 'OK' and msg_data and msg_data[0]:
                    raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                    if raw_email:
                        msg = email.message_from_bytes(raw_email)
                        
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


def get_spam_folder(imap_conn):
    """Dynamically locates the Spam/Junk folder using attributes or common names."""
    status, folder_list = imap_conn.list()
    if status != 'OK':
        return None
    
    # Priority 1: Official \Junk IMAP attribute
    for f_info in folder_list:
        f_str = f_info.decode('utf-8', 'ignore')
        if r'\Junk' in f_str:
            match = re.search(r'\((?P<attrs>.*)\)\s+"(?P<delim>.*)"\s+"?(?P<name>.*)"?', f_str)
            if match:
                return match.group('name').strip('"')

    # Priority 2: Regex match on common provider names
    spam_names = ['spam', 'junk', '[gmail]/spam', 'bulk mail', 'junk email']
    for f_info in folder_list:
        f_str = f_info.decode('utf-8', 'ignore')
        match = re.search(r'\((?P<attrs>.*)\)\s+"(?P<delim>.*)"\s+"?(?P<name>.*)"?', f_str)
        if match:
            folder_name = match.group('name').strip('"')
            folder_lower = folder_name.lower()
            if any(name in folder_lower for name in spam_names):
                return folder_name
                
    return None


def rescue_from_spam(imap_conn):
    """
    Locates the Spam folder, searches for the header, moves to INBOX.
    Returns the integer count of rescued emails.
    """
    rescued_count = 0
    try:
        spam_folder = get_spam_folder(imap_conn) # Reuse your existing utility
        if not spam_folder: return 0

        status, _ = imap_conn.select(f'"{spam_folder}"', readonly=False)
        if status == 'OK':
            status, data = imap_conn.uid('search', None, '(UNSEEN HEADER "X-Warmup-ID" "")')
            if status == 'OK' and data[0]:
                uids = data[0].split()
                rescued_count = len(uids)
                
                for target_uid in uids:
                    # Move to INBOX
                    imap_conn.uid('store', target_uid, '-FLAGS', '\\Junk')
                    imap_conn.uid('store', target_uid, '+FLAGS', '$NotJunk')
                    copy_status = imap_conn.uid('copy', target_uid, '"INBOX"')
                    
                    if copy_status[0] == 'OK':
                        imap_conn.uid('store', target_uid, '+FLAGS', '\\Deleted')
                        
                imap_conn.expunge()
    except Exception as e:
        print(f"IMAP Rescue Error: {e}")
        
    return rescued_count


