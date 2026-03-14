import random
import re
import smtplib
import imaplib
import time
import email
from django.core.mail import get_connection
from users.models import EmailAccount
from django.db.models import Count, Q
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


# IMAP_SETTINGS_MAP = {
#     'gmail':     {'host': 'imap.gmail.com', 'port': 993},
#     'outlook':   {'host': 'outlook.office365.com', 'port': 993},
#     'yahoo':     {'host': 'imap.mail.yahoo.com', 'port': 993},
#     'zoho':      {'host': 'imap.zoho.com', 'port': 993},
#     'hostinger': {'host': 'imap.hostinger.com', 'port': 993},
#     'namecheap': {'host': 'imap.privateemail.com', 'port': 993},
#     'godaddy':  {'host': 'imap.secureserver.net', 'port': 993},
#     'titan':     {'host': 'imap.titan.email', 'port': 993},
# }

# A professional master template with placeholders for injection

# STEP 0: The Hook (Short, low pressure)
WARMUP_STEP_0 = """
{Hi|Hello|Hey} {recipient_name}, {quick question --|just curious,|reaching out because} 
{do you have any {recommendations|thoughts} on {software|tools|vendors} for {logistics|ops|growth}?|
{I saw|Came across} a post about {industry trends|supply chains} and {wondered|was curious} if your team is {seeing|experiencing} the same thing.|
{Are you|Is your team} {open to|available for} a {quick|short} {chat|sync} about {standardizing processes|improving workflows}?}
{Best,|Thanks,|Cheers,} {sender_name}
"""

# STEP 1: The Casual Reply (Target's Turn)
WARMUP_STEP_1 = """
{Hey|Hi} {sender_name}, {interesting|good} question. {We've been {looking into|testing} a few things lately.|I'll have to {check with|ask} the team on that one.} 
{What's your {timeline|primary focus} right now?|{Are you|Is your team} seeing any specific {issues|bottlenecks}?}
{Talk soon,|Regards,} {recipient_name}
"""

# STEP 2: The Specifics (Sender's Turn)
WARMUP_STEP_2 = """
{Thanks for getting back to me.|Appreciate the reply.} {Mainly|Mostly} {curious|focused} on {how you're handling {data|shipping|scaling}|the {current|upcoming} shift in {operations|market demand}}. 
{I heard|Read somewhere} that {automation|better visibility} is {making a huge difference|the big focus this year}. {Is that {the case|true} for you guys?|{What do you think|Thoughts}?}
{Best,} {sender_name}
"""

# STEP 3: The "Me Too" Validation (Target's Turn)
WARMUP_STEP_3 = """
{Definitely.|Spot on.|I agree.} {It's been a {priority|challenge} for us {as well|lately}.|We've noticed {the same thing|similar trends} with our {clients|partners}.} 
{Actually,|By the way,} {do you use|have you tried} {any specific {platforms|dashboards}|that one {tool|system} everyone is talking about}? 
{Cheers,} {recipient_name}
"""

# STEP 4: The Resource/Meeting (Sender's Turn)
WARMUP_STEP_4 = """
{I haven't tried that yet,|We're actually using something else,|Good to know,} {but I'll {look into it|check it out}.| {actually|honestly} it sounds {interesting|promising}.}
{If you're {free|available} {later this week|next week}, {maybe we could|perhaps we can} {sync|connect} for 10 minutes?| {I can|I'd love to} {share|send over} a {quick doc|resource} we've been using for {this|that workflow}.}
{Let me know,|Talk soon,} {sender_name}
"""

# STEP 5: The "Close" (Target's Turn)
WARMUP_STEP_5 = """
{That {sounds|seems} {fair|great}.|{Sure,|Yeah,} that works.} {Send it over|Send that my way} {and I'll {take a look|review it}|when you have a chance}. 
{Let's {touch base|chat} {then|sometime soon}.|Looking forward to it.}
{Best,|Regards,} {recipient_name}
"""

WARMUP_SUBJECT_TEMPLATE = "{" \
"{Quick|Short|Small} {question|note|thought} for {you|{recipient_name}}" \
"|{Hello|Hi} {recipient_name}" \
"|{Checking in|Touching base|Quick follow-up}" \
"|{Thoughts on|Quick note about} {operations|growth|recent trends|this week}" \
"|{Are you seeing this too?|Quick industry question}" \
"|{Quick idea|Small thought} about {business processes|team workflows|growth}" \
"|{Connecting|Reaching out} from {sender_company}" \
"|{Quick chat|Short call} sometime {this week|next week}?" \
"|{Curious about|Quick question on} {your workflow|your process|your current setup}" \
"|{A quick hello|Just saying hi} from {sender_company}" \
"}"


def spin_text(text):
    """
    Parses Spintax format: {Option A|Option B|Option C}
    Supports nested spintax: {Hi|Hello {friend|colleague}}
    """
    # Regex to find the innermost set of braces: { ... } containing no other { }
    pattern = re.compile(r'\{([^{}]+)\}')
    
    while True:
        match = pattern.search(text)
        if not match:
            break
            
        # The full match is "{A|B}". Group 1 is "A|B"
        full_match = match.group(0)
        content = match.group(1)
        
        choices = content.split('|')
        selected = random.choice(choices)
        
        # Replace ONLY the first occurrence of this specific match
        text = text.replace(full_match, selected, 1)
        
    return text


def get_template_for_step(step_number):
    templates = {
        0: WARMUP_STEP_0,
        1: WARMUP_STEP_1,
        2: WARMUP_STEP_2,
        3: WARMUP_STEP_3,
        4: WARMUP_STEP_4,
        5: WARMUP_STEP_5,
    }
    # Fallback to Step 1 if something goes wrong with high step numbers
    return templates.get(step_number, WARMUP_STEP_1)


def generate_spintax_body(recipient_first_name, sender_company_name, step_number):
    """
    Generates a coherent, unique email body using Spintax.
    """
    # 1. Get the master template
    raw_text = get_template_for_step(step_number)
    
    # 2. Inject Variables BEFORE spinning (so they don't break the syntax)
    # We use explicit replace or f-string logic
    raw_text = raw_text.replace("{recipient_name}", recipient_first_name or "there")
    raw_text = raw_text.replace("{sender_company}", sender_company_name or "Company")

    # 3. Spin it
    final_body = spin_text(raw_text)
    
    return final_body


def generate_spintax_subject(recipient_first_name=None, sender_company_name=None):
    """
    Generates a realistic B2B subject line using Spintax.
    """
    raw_text = WARMUP_SUBJECT_TEMPLATE
    
    # Inject variables if they exist, otherwise fallback to generic terms
    # using 'title()' for names looks more professional in subjects
    r_name = recipient_first_name.title() if recipient_first_name else "there"
    s_company = sender_company_name if sender_company_name else "our project"

    # Clean injection
    raw_text = raw_text.replace("{recipient_name}", r_name)
    raw_text = raw_text.replace("{sender_company}", s_company)
    
    # Spin
    return spin_text(raw_text)


def refresh_targets(campaign):
    TARGET_LIMIT = 3       # Reduced for safety
    MEMBERSHIP_CAP = 5     # Maximum 5 campaigns as a target
    DAILY_VELOCITY_CAP = 10 # 10 incoming emails max per day
    
    sender_account = campaign.sender_account
    sender_user = sender_account.user
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        with transaction.atomic():
            base_qs = EmailAccount.objects.filter(
                black_list=False, 
                is_warmup_target=True
            ).exclude(user=sender_user)

            eligible_ids = list(
                base_qs.annotate(
                    active_target_count=Count(
                        'target_of_warmup_campaigns',
                        filter=Q(target_of_warmup_campaigns__status='Active'),
                        distinct=True
                    ),
                    received_today_count=Count(
                        'received_warmup_messages',
                        filter=Q(received_warmup_messages__sent_at__gte=today_start),
                        distinct=True
                    )
                ).filter(
                    active_target_count__lt=MEMBERSHIP_CAP,
                    received_today_count__lt=DAILY_VELOCITY_CAP
                ).order_by('active_target_count', 'received_today_count', '?')
                .values_list('id', flat=True)[:100]
            )

            if not eligible_ids:
                # If total starvation occurs, stagger the retry
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 4))
                campaign.save(update_fields=['next_action_at'])
                return []

            # Lock the best available rows
            selected_accounts = list(
                EmailAccount.objects.filter(id__in=eligible_ids)
                .select_for_update(skip_locked=True)[:TARGET_LIMIT]
            )

            if selected_accounts:
                campaign.target_accounts.set(selected_accounts)
                return selected_accounts
            
            return []

    except Exception as e:
        print(f"Error in refresh_targets for campaign {campaign.id}: {e}")
        return []


# def get_humanized_delay():
#     now = timezone.now()
#     day_of_week = now.weekday() # 0=Mon, 5=Sat, 6=Sun
    
#     # 1. Base Logic: Random 2 to 7 hours
#     delay_hours = random.uniform(2, 7)
#     next_action = now + timedelta(hours=delay_hours)
    
#     # 2. Weekend Logic (Saturday/Sunday)
#     if day_of_week >= 5:
#         # Saturday: Target 10 AM - 1 PM
#         if day_of_week == 5:
#             target_hour = random.randint(10, 13)
#         # Sunday: Target 7 PM - 10 PM
#         else:
#             target_hour = random.randint(19, 22)
            
#         # If the delay lands outside the target, push it to the next day's target window
#         if next_action.hour < target_hour:
#             delay_hours += (target_hour - next_action.hour)
#         else:
#             delay_hours += (24 - next_action.hour + target_hour)
#         return delay_hours

#     # 3. Weekday Logic (9 AM - 6 PM)
#     # If the delay pushes into the "Night" (after 6 PM)
#     if next_action.hour >= 18 or next_action.hour < 9:
#         # Calculate hours until 9 AM the next morning
#         if next_action.hour >= 18:
#             hours_to_9am = (24 - next_action.hour) + 9
#         else:
#             hours_to_9am = 9 - next_action.hour
            
#         # Add a random "start of day" jitter (9 AM to 11 AM)
#         delay_hours += hours_to_9am + random.uniform(0, 2)
        
#     return delay_hours

def get_humanized_delay():
    """
    Returns an absolute datetime for the next action, 
    strictly enforcing 9AM-6PM weekdays and custom weekend windows.
    """
    now = timezone.now()
    day_of_week = now.weekday() # 0=Mon, 5=Sat, 6=Sun
    
    # 1. Base Logic: Random 2 to 4 hour delay
    base_delay = random.uniform(2, 4)
    target_time = now + timedelta(hours=base_delay)
    
    # 2. Weekend Logic (Saturday/Sunday)
    if day_of_week >= 5:
        # Sat Target: 10AM-1PM | Sun Target: 7PM-10PM
        t_hour = random.randint(10, 13) if day_of_week == 5 else random.randint(19, 22)
        
        # Lock to the target hour with random minutes/seconds
        target_time = target_time.replace(
            hour=t_hour, 
            minute=random.randint(0, 59), 
            second=random.randint(0, 59)
        )
        
        # If the target hour for today has already passed, move to tomorrow's target
        if target_time < now:
            target_time += timedelta(days=1)
        return target_time

    # 3. Weekday Logic (9 AM - 6 PM)
    # If the target lands in the "Night" (after 6 PM or before 9 AM)
    if target_time.hour >= 18 or target_time.hour < 9:
        # Move to tomorrow morning
        tomorrow = now + timedelta(days=1)
        # Start at 9 AM and add a random jitter up to 2 hours (9 AM - 11 AM)
        target_time = tomorrow.replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + timedelta(minutes=random.randint(0, 120))
        
    return target_time


def personalize_template(template, lead):
    
    # Find all placeholders like [Some Column]
    placeholders = re.findall(r'\[([^\]]+)\]', template)
    
    for ph in placeholders:
        value = str(lead.get(ph, ph))  # Use the column name as fallback if missing
        template = template.replace(f"[{ph}]", value)
    
    return template

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


# def normalize_provider(provider_string):
#     """
#     Cleans the user-entered provider string to match a key in IMAP_SETTINGS_MAP.
#     """
#     if not provider_string:
#         return None
        
#     provider_low = provider_string.lower()
    
#     if 'gmail' in provider_low or 'google' in provider_low:
#         return 'gmail'
#     if 'outlook' in provider_low or 'microsoft' in provider_low:
#         return 'outlook'
#     if 'yahoo' in provider_low:
#         return 'yahoo'
#     if 'zoho' in provider_low:
#         return 'zoho'
#     if 'hostinger' in provider_low:
#         return 'hostinger'
#     if 'namecheap' in provider_low or 'privateemail' in provider_low:
#         return 'namecheap'
#     if 'godaddy' in provider_low or 'secureserver' in provider_low:
#         return 'godaddy'
#     if 'titan' in provider_low:
#         return 'titan'
        
#     return None


def get_warmup_imap_connection(email_account):
    """
    Establishes IMAP connection using standardized database fields.
    """
    # 1. Pull directly from the new model fields
    imap_host = email_account.imap_host
    imap_port = email_account.imap_port or 993

    # 2. Last-resort fallback if fields are somehow empty
    if not imap_host and email_account.host:
        if email_account.host.startswith('smtp.'):
            imap_host = email_account.host.replace('smtp.', 'imap.', 1)
    
    if not imap_host:
        print(f"[IMAP Error] No IMAP host found for {email_account.email_address}")
        return None

    try:
        # 3. Connection attempt
        imap_conn = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=30)
        decrypted_password = email_account.get_password()
        
        if not decrypted_password:
            print(f"[IMAP Error] Could not decrypt password for {email_account.email_address}")
            return None
            
        imap_conn.login(email_account.email_address, decrypted_password)
        return imap_conn
        
    except (imaplib.IMAP4.error, TimeoutError, OSError) as e:
        print(f"Warmup IMAP Connection Failed for {email_account.email_address}: {e}")
        return None


# def ensure_folder_exists(imap_conn, folder_name="Warmup"):
#     try:
#         status, folders = imap_conn.list()
#         if status != 'OK': return False
        
#         folder_exists = False
#         for f in folders:
#             decoded_f = f.decode('utf-8', 'ignore')
#             # Look for the folder name at the end of the string (after the last delimiter)
#             if decoded_f.strip().endswith(f'"{folder_name}"') or decoded_f.strip().endswith(f' {folder_name}'):
#                 folder_exists = True
#                 break
        
#         if not folder_exists:
#             # Create the folder. Quoting is essential.
#             status, res = imap_conn.create(f'"{folder_name}"')
#             if status == 'OK':
#                 imap_conn.subscribe(f'"{folder_name}"')
#                 return True
#             return False
#         return True
#     except:
#         return False


# def check_inbox_and_rescue(email_account, target_message_id):
#     """
#     Finds a message by ID across all folders, rescues it from Spam/Junk if needed,
#     and moves it to the 'Warmup' folder. 
    
#     Hardened for: Gmail Labels, Hostinger Delimiters, and IMAP State Safety.
#     """
#     TARGET_FOLDER = "Warmup"
#     # Normalize Message-ID for strict header searching
#     search_id = target_message_id if target_message_id.startswith('<') else f'<{target_message_id}>'
    
#     imap_conn = get_warmup_imap_connection(email_account)
#     if not imap_conn: 
#         return None

#     try:
#         # 0. Ensure target folder exists before doing anything
#         ensure_folder_exists(imap_conn, TARGET_FOLDER)
        
#         # 1. Map all available folders
#         status, folder_list = imap_conn.list()
#         if status != 'OK': 
#             return None

#         # System folders and non-selectable folders to skip
#         skip_attrs = [r'\Sent', r'\Trash', r'\Drafts', r'\Deleted', r'\Noselect']
#         found_uid, found_in_folder = None, None

#         for f_info in folder_list:
#             f_str = f_info.decode('utf-8', 'ignore')
            
#             # Optimization: Skip folders that are explicitly non-selectable or system-specific
#             if any(attr in f_str for attr in skip_attrs):
#                 continue

#             # Parse folder name regardless of provider delimiter (regex handles "." and "/")
#             match = re.search(r'\((?P<attrs>.*)\)\s+"(?P<delim>.*)"\s+"?(?P<name>.*)"?', f_str)
#             if not match: 
#                 continue
            
#             current_folder = match.group('name').strip('"')
            
#             try:
#                 # CRITICAL: Attempt to SELECT. We must be in SELECTED state for SEARCH.
#                 res, _ = imap_conn.select(f'"{current_folder}"', readonly=False)
#                 if res != 'OK':
#                     # If selection fails, we stay in AUTH state; skipping search to prevent illegal command error
#                     continue

#                 # Tier 1: Strict Header Search
#                 res, data = imap_conn.uid('search', None, f'HEADER Message-ID "{search_id}"')
#                 uids = data[0].split()

#                 # Tier 2: Fuzzy Body Search (fallback)
#                 if not uids:
#                     res, data = imap_conn.uid('search', None, f'TEXT "{target_message_id}"')
#                     uids = data[0].split()

#                 if uids:
#                     found_uid = uids[-1]
#                     found_in_folder = current_folder
#                     break 
#             except:
#                 continue

#         if not found_uid: 
#             return None

#         # 2. Rescue Logic (Move to Warmup if found elsewhere)
#         if found_in_folder != TARGET_FOLDER:
#             # Atomic Move pattern: Copy -> Flag -> Expunge
#             copy_res = imap_conn.uid('copy', found_uid, f'"{TARGET_FOLDER}"')
#             if copy_res[0] == 'OK':
#                 imap_conn.uid('store', found_uid, '+FLAGS', '\\Deleted')
#                 imap_conn.expunge()
            
#             # Re-Select Warmup and find the NEW UID for the message
#             res, _ = imap_conn.select(f'"{TARGET_FOLDER}"')
#             if res == 'OK':
#                 res, data = imap_conn.uid('search', None, f'HEADER Message-ID "{search_id}"')
#                 new_uids = data[0].split()
#                 if new_uids:
#                     found_uid = new_uids[-1]
#                 else:
#                     # Message moved but indexer hasn't caught up
#                     print(f"[IMAP Warning] Moved {search_id} to {TARGET_FOLDER}, but not found in index yet.")
#                     return None
#             else:
#                 return None

#         # 3. Content Extraction (with None payload safety)
#         status, msg_data = imap_conn.uid('fetch', found_uid, "(RFC822)")
#         if status != 'OK' or not msg_data: 
#             return None

#         raw_email = msg_data[0][1]
#         msg = email.message_from_bytes(raw_email)
        
#         body = ""
#         if msg.is_multipart():
#             for part in msg.walk():
#                 if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
#                     payload = part.get_payload(decode=True)
#                     if payload:
#                         body = payload.decode(errors='replace')
#                         break
#         else:
#             payload = msg.get_payload(decode=True)
#             if payload:
#                 body = payload.decode(errors='replace')
            
#         return body

#     except Exception as e:
#         print(f"Rescue Error [{email_account.email_address}]: {e}")
#         return None
#     finally:
#         # Standardize closure
#         try:
#             # imap_conn.close() only works if we are in SELECTED state
#             imap_conn.logout()
#         except:
#             pass


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



def check_inbox_and_rescue(imap_conn, target_message_id):
    """
    Checks INBOX first for the message to extract the body.
    If not found, locates the Spam folder, rescues the message to INBOX, and extracts the body.
    Requires an active imap_conn.
    """
    search_id = target_message_id if target_message_id.startswith('<') else f'<{target_message_id}>'
    
    # Re-adding your original connection handler
    if not imap_conn: 
        return None

    try:
        # --- 1. Check INBOX First (The Happy Path) ---
        status, _ = imap_conn.select('"INBOX"', readonly=True)
        if status == 'OK':
            status, data = imap_conn.uid('search', None, f'HEADER Message-ID "{search_id}"')
            if status == 'OK' and data[0]:
                uids = data[0].split()
                if uids:
                    target_uid = uids[-1]
                    status, msg_data = imap_conn.uid('fetch', target_uid, "(RFC822)")
                    if status == 'OK' and msg_data and msg_data[0]:
                        raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if raw_email:
                            msg = email.message_from_bytes(raw_email)
                            return extract_text_body(msg)

        # --- 2. Check Spam Folder (The Rescue Path) ---
        spam_folder = get_spam_folder(imap_conn)
        if not spam_folder:
            return None

        status, _ = imap_conn.select(f'"{spam_folder}"', readonly=False)
        if status == 'OK':
            status, data = imap_conn.uid('search', None, f'HEADER Message-ID "{search_id}"')
            if status == 'OK' and data[0]:
                uids = data[0].split()
                if uids:
                    target_uid = uids[-1]

                    # Extract the Body
                    status, msg_data = imap_conn.uid('fetch', target_uid, "(RFC822)")
                    body = None
                    if status == 'OK' and msg_data and msg_data[0]:
                        raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if raw_email:
                            msg = email.message_from_bytes(raw_email)
                            body = extract_text_body(msg)

                    # Execute Rescue (Not Spam) Protocol
                    imap_conn.uid('store', target_uid, '-FLAGS', '\\Junk')
                    imap_conn.uid('store', target_uid, '+FLAGS', '$NotJunk')

                    copy_status = imap_conn.uid('copy', target_uid, '"INBOX"')
                    if copy_status[0] == 'OK':
                        imap_conn.uid('store', target_uid, '+FLAGS', '\\Deleted')
                        imap_conn.expunge()

                        # 4. Apply "Trust Signals" to the new copy in INBOX
                        # We must re-select INBOX in write mode to apply flags
                        imap_conn.select('"INBOX"', readonly=False)
                        time.sleep(1)
                        res, search_data = imap_conn.uid('search', None, f'HEADER Message-ID "{search_id}"')
                        
                        if res == 'OK' and search_data[0]:
                            new_uid = search_data[0].split()[-1]
                            
                            # SIGNAL A: Mark as \Important (High signal for Gmail/Outlook)
                            imap_conn.uid('store', new_uid, '+FLAGS', '\\Important')
                            
                            # SIGNAL B: Mark as \Flagged (Starred)
                            imap_conn.uid('store', new_uid, '+FLAGS', '\\Flagged')
                            
                            # SIGNAL C: Human Read Emulation (Unseen -> wait -> Seen)
                            imap_conn.uid('store', new_uid, '-FLAGS', '\\Seen')
                            time.sleep(random.uniform(2.0, 5.0))
                            imap_conn.uid('store', new_uid, '+FLAGS', '\\Seen')

                    return body

        # Not found in INBOX or SPAM
        return None

    except Exception as e:
        print(f"Rescue Error: {e}")
        return None


def process_audit_results(results_map):
    """
    Parses API results. If an account is 'undeliverable' or has a low score:
    1. Mark it as blacklisted.
    2. Stop its own campaigns.
    3. Remove it from everyone else's target lists immediately.
    """
    blacklisted_count = 0
    
    for email, data in results_map.items():
        status = str(data.get('status', '')).lower()
        score = data.get('score', 0)
        
        # Strict Rule: Undeliverable OR Score <= 40
        if status == 'undeliverable':
            try:
                # Lock the row to prevent race conditions
                account = EmailAccount.objects.select_related('user').get(email_address__iexact=email)
                
                if not account.black_list:
                    print(f"Blacklisting {email} | Status: {status}, Score: {score}")
                    
                    # 1. Update Flags
                    account.black_list = True
                    account.is_warmup_target = False
                    account.save(update_fields=['black_list', 'is_warmup_target'])
                    
                    # 2. Stop this account's OWN campaigns
                    account.warmup_campaigns.filter(status='Active').update(status='Complete')

                    # 3. Remove this account from OTHER campaigns
                    target_campaigns = account.target_of_warmup_campaigns.all()
                    
                    if target_campaigns.exists():
                        print(f"Removing {email} from {target_campaigns.count()} external campaigns.")
                        for campaign in target_campaigns:
                            campaign.target_accounts.remove(account)

                    blacklisted_count += 1

            except EmailAccount.DoesNotExist:
                continue

    if blacklisted_count > 0:
        print(f"Audit Complete: Cleaned up {blacklisted_count} accounts.")

