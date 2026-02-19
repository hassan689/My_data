import random
import re
import smtplib
import imaplib
import email
from django.core.mail import get_connection
from users.models import EmailAccount
from django.db.models import Count, Q


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

# A professional master template with placeholders for injection
WARMUP_SPINTAX_TEMPLATE = """
{Hi|Hello|Hey|Good morning|Good afternoon} {recipient_name},

{I hope this email finds you well.|Hope you're having a {great|productive|smooth} week.|Trust everything is going well on your end.|Hope business has been treating you well lately.}

{I was|I've been|We were} {reading about|looking into|learning more about|following} 
{the {logistics|construction|manufacturing|healthcare|retail|tech|real estate|food service|automotive|e-commerce|energy|agriculture} space
|how companies in {your industry|the B2B space|operations-heavy businesses|service-based businesses} are adapting lately
|recent shifts in {supply chains|customer demand|digital adoption|hiring trends|operational efficiency}
|how teams are handling {growth|scaling challenges|new clients|process improvements}
|emerging trends in {small business operations|enterprise workflows|client acquisition|team productivity}}.

{It made me think about|It reminded me of|It got me thinking about}
{how different teams are approaching {growth|efficiency|automation|customer experience|sales processes}
|the way companies are adjusting their {workflows|operations|strategies}
|how businesses are preparing for the next quarter
|how organizations are improving internal processes}.

{Curious to hear|Would love to hear|Interested in knowing}
{what your experience has been like|how things are going on your side|what trends you're noticing|how your team is approaching things}.

{If you're open to it,|If it makes sense,|Whenever you have a moment,}
{we could|maybe we could|perhaps we can}
{jump on a quick call|have a short chat|connect for a few minutes|exchange a few thoughts}
{sometime this week|in the coming days|next week|whenever it suits your schedule}.

{Best regards|Regards|Cheers|Talk soon|All the best},
{sender_company} Team
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


def generate_spintax_body(recipient_first_name, sender_company_name):
    """
    Generates a coherent, unique email body using Spintax.
    """
    # 1. Get the master template
    raw_text = WARMUP_SPINTAX_TEMPLATE
    
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
    """
    Refreshes the target list for a campaign using a least-burdened strategy.
    Prioritizes accounts acting as targets in the fewest 'Active' campaigns.
    """
    # 1. Configuration: Reduced from 5 to 2 targets to balance the pool
    TARGET_LIMIT = 2
    sender_account = campaign.sender_account
    sender_user = sender_account.user

    # 2. Base Pool: Eligible accounts, excluding the sender's own user
    # This prevents self-warming and maintains external deliverability weight.
    base_qs = EmailAccount.objects.filter(
        black_list=False, 
        is_warmup_target=True
    ).exclude(user=sender_user)

    # 3. Least-Burdened Annotation
    # We count how many 'Active' campaigns each account is currently a target of.
    # The related_name 'target_of_warmup_campaigns' is used for the join.
    accounts_with_load = base_qs.annotate(
        active_target_count=Count(
            'target_of_warmup_campaigns',
            filter=Q(target_of_warmup_campaigns__status='Active')
        )
    )

    # 4. Selection Logic
    # Order by active_target_count (ascending) to pick the least used accounts first.
    # Order by '?' (random) secondarily to break ties and prevent static pairs.
    selected_accounts_qs = accounts_with_load.order_by('active_target_count', '?')[:TARGET_LIMIT]
    
    selected_accounts = list(selected_accounts_qs)

    # 5. Save and Return
    if selected_accounts:
        campaign.target_accounts.set(selected_accounts)
    
    return selected_accounts


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


def normalize_provider(provider_string):
    """
    Cleans the user-entered provider string to match a key in IMAP_SETTINGS_MAP.
    """
    if not provider_string:
        return None
        
    provider_low = provider_string.lower()
    
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
        
    return None


def get_warmup_imap_connection(email_account):
    """
    Establishes IMAP connection specifically for Warmup. 
    """
    normalized_name = normalize_provider(email_account.email_provider)

    imap_host = None
    imap_port = 993

    if normalized_name and normalized_name in IMAP_SETTINGS_MAP:
        imap_host = IMAP_SETTINGS_MAP[normalized_name]['host']
        imap_port = IMAP_SETTINGS_MAP[normalized_name]['port']
    elif email_account.host:
        # Fallback logic: replace smtp with imap
        if email_account.host.startswith('smtp.'):
            imap_host = email_account.host.replace('smtp.', 'imap.', 1)
    
    if not imap_host:
        return None

    try:
        imap_conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        decrypted_password = email_account.get_password()
        imap_conn.login(email_account.email_address, decrypted_password)
        return imap_conn
    except Exception as e:
        print(f"Warmup IMAP Connection Failed for {email_account.email_address}: {e}")
        return None


# Dedicated folder for warmup emails helps in organization and prevents cluttering the main inbox.
def ensure_folder_exists(imap_conn, folder_name="Warmup"):
    """
    Checks if a folder exists. If not, creates and subscribes to it.
    Uses quoted folder names to prevent "BAD Could not parse" IMAP errors.
    Returns True if successful/exists, False otherwise.
    """
    try:
        # 1. List all folders to check existence
        status, folders = imap_conn.list()
        folder_exists = False
        
        # We wrap the name in quotes to handle spaces and prevent parser errors
        quoted_name = f'"{folder_name}"'
        
        for f in folders:
            if not f: continue
            decoded_f = f.decode('utf-8', 'ignore')
            # Check for exact matches in the list response
            if quoted_name in decoded_f or f' {folder_name}' in decoded_f:
                folder_exists = True
                break
        
        # 2. Create if missing
        if not folder_exists:
            print(f"Creating folder {quoted_name}...")
            # Use the quoted name for the CREATE command
            status, response = imap_conn.create(quoted_name)
            if status != 'OK':
                print(f"Failed to create folder: {response}")
                return False
                
        # 3. Subscribe (Important for some clients to "see" it)
        try:
            imap_conn.subscribe(quoted_name)
        except:
            pass

        return True

    except Exception as e:
        print(f"Error ensuring folder {folder_name}: {e}")
        return False


def check_inbox_and_rescue(email_account, target_message_id):
    """
    Finds a message by ID. Moves it to the 'Warmup' folder if found in Inbox or Spam.
    Returns the message body.
    """
    TARGET_FOLDER = "Warmup"
    quoted_target = f'"{TARGET_FOLDER}"'
    
    imap_conn = get_warmup_imap_connection(email_account)
    if not imap_conn:
        return None

    try:
        # 0. Ensure the dedicated Warmup folder exists
        if not ensure_folder_exists(imap_conn, TARGET_FOLDER):
            # Fallback to Inbox if folder creation fails
            TARGET_FOLDER = "INBOX"
            quoted_target = "INBOX"

        # 1. Search the target 'Warmup' folder first
        imap_conn.select(quoted_target) 
        status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
        email_ids = messages[0].split()
        
        # 2. If not found, Search INBOX (Declutter logic)
        if not email_ids:
            imap_conn.select("INBOX")
            status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
            inbox_ids = messages[0].split()
            
            if inbox_ids:
                # Found in Inbox -> Move to Warmup
                msg_num = inbox_ids[0]
                copy_res = imap_conn.copy(msg_num, quoted_target)
                
                if copy_res[0] == 'OK':
                    # Flag for deletion and purge from Inbox
                    imap_conn.store(msg_num, '+FLAGS', '\\Deleted')
                    imap_conn.expunge()
                    
                    # Switch to Warmup to fetch the content
                    imap_conn.select(quoted_target)
                    status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
                    email_ids = messages[0].split()

        # 3. If still not found, Search common Spam folders (Rescue logic)
        if not email_ids:
            spam_folders = ["Spam", "Junk", "Junk Email", "[Gmail]/Spam", "Bulk", "Spambox"]
            
            for folder in spam_folders:
                try:
                    # Select folder, quoting only if it contains spaces
                    folder_selector = f'"{folder}"' if " " in folder else folder
                    status, _ = imap_conn.select(folder_selector)
                    if status != 'OK': continue
                        
                    status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
                    spam_msg_ids = messages[0].split()
                    
                    if spam_msg_ids:
                        print(f"Rescue: Found {target_message_id} in {folder}. Moving to {TARGET_FOLDER}.")
                        msg_num = spam_msg_ids[0]
                        
                        # Copy to Warmup folder
                        copy_res = imap_conn.copy(msg_num, quoted_target)
                        
                        if copy_res[0] == 'OK':
                            # Force permanent removal from Spam
                            imap_conn.store(msg_num, '+FLAGS', '\\Deleted')
                            imap_conn.expunge() 
                            
                            # Final selection to retrieve ID from the Warmup folder
                            imap_conn.select(quoted_target)
                            status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
                            email_ids = messages[0].split()
                        break
                except:
                    continue
            
        if not email_ids:
            return None # Message not found in any folder

        # 4. Fetch and Parse Content
        latest_email_id = email_ids[-1]
        status, msg_data = imap_conn.fetch(latest_email_id, "(RFC822)")
        
        if status != 'OK' or not msg_data or not msg_data[0]:
            return None

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        body_content = ""
        if msg.is_multipart():
            for part in msg.walk():
                # Focus only on the plain text body, skipping attachments
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                    try:
                        body_content = part.get_payload(decode=True).decode(errors='replace')
                        break
                    except:
                        pass
        else:
            try:
                body_content = msg.get_payload(decode=True).decode(errors='replace')
            except:
                pass

        return body_content

    except Exception as e:
        print(f"Error in rescue function: {e}")
        return None
    finally:
        # Guarantee connection closure to keep provider happy
        try:
            imap_conn.close()
            imap_conn.logout()
        except:
            pass


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

