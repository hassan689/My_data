import random
import re
import smtplib
import imaplib
import email
from django.core.mail import get_connection
from users.models import EmailAccount
from django.core.mail import send_mail
from django.conf import settings


IMAP_SETTINGS_MAP = {
    'gmail':     {'host': 'imap.gmail.com', 'port': 993},
    'outlook':   {'host': 'outlook.office365.com', 'port': 993},
    'yahoo':     {'host': 'imap.mail.yahoo.com', 'port': 993},
    'zoho':      {'host': 'imap.zoho.com', 'port': 993},
    'hostinger': {'host': 'imap.hostinger.com', 'port': 993},
    'namecheap': {'host': 'imap.privateemail.com', 'port': 993},
}

# A professional master template with placeholders for injection
WARMUP_SPINTAX_TEMPLATE = """
{Hi|Hello|Hey} {recipient_name},

{I hope this email finds you well.|I hope you are having a {great|productive} week.|Trust you're doing well.}

{I wanted to|Just wanted to} {reach out|connect|touch base} {briefly|quickly} regarding {our previous discussion|a potential partnership|some updates on our end|the business landscape}. {We have been|My team has been} {working on|reviewing} {some new strategies|internal processes|the latest market trends} and {I thought|I figured} it might be {relevant|of interest|useful} to {you|your team}.

{Are you available|Do you have time} for a {quick|brief} {call|chat|discussion} {sometime soon|this week|next week}? {I'd love to|It would be great to} {hear your thoughts|get your input|catch up}.

{Best|Regards|Cheers|Talk soon},
{sender_company} Team
"""

WARMUP_SUBJECT_TEMPLATE = "{" \
                          "{Quick|Brief} {question|inquiry|query} for {you|{recipient_name}}" \
                          "|{Connecting|Touching base|Checking in} regarding {business|{sender_company}|potential partnership}" \
                          "|{Thoughts|Feedback} on {this|latest updates|our proposal}?" \
                          "|{Meeting|Call} {request|invitation}: {Next week|This week}?" \
                          "|{Important|Update}: {Regarding your account|Project details|Next steps}" \
                          "|{Hello|Hi} {recipient_name}, {quick question|got a minute?}" \
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
    Refreshes the target list for a campaign.
    Priority 1: 'Idle' accounts (Not currently a target in any ACTIVE campaign).
    Priority 2: 'Busy' accounts (Already targeted, used as fill-in).
    """
    target_count = 5
    sender_account = campaign.sender_account
    sender_user = sender_account.user

    # 1. Base Pool: Eligible accounts, excluding the sender's own user
    # We exclude the sender_user entirely to prevent self-warming loops within one user's account
    base_qs = EmailAccount.objects.filter(
        black_list=False, 
        is_warmup_target=True
    ).exclude(user=sender_user)

    # 2. Priority Pool: Find accounts that are NOT in any 'Active' campaign right now
    # We use the related_name 'target_of_warmup_campaigns' to check status
    idle_accounts_qs = base_qs.exclude(target_of_warmup_campaigns__status='Active')
    idle_accounts = list(idle_accounts_qs)

    selected_accounts = []

    # 3. Selection Logic
    if len(idle_accounts) >= target_count:
        # Ideal: We have enough idle accounts to fill the slots
        selected_accounts = random.sample(idle_accounts, target_count)
    else:
        # Scarcity: Take all idle accounts, then fill the remainder with busy ones
        selected_accounts = idle_accounts[:] # Take them all
        needed = target_count - len(selected_accounts)
        
        if needed > 0:
            
            # Get IDs of accounts we already selected to exclude them
            selected_ids = [acc.id for acc in selected_accounts]
            
            busy_accounts_qs = base_qs.filter(
                target_of_warmup_campaigns__status='Active'
            ).exclude(id__in=selected_ids).distinct()
            
            busy_accounts = list(busy_accounts_qs)

            # Fill the rest
            if len(busy_accounts) >= needed:
                selected_accounts.extend(random.sample(busy_accounts, needed))
            else:
                # If we still don't have enough, just take what exists
                selected_accounts.extend(busy_accounts)

    # 4. Save and Return
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
def ensure_folder_exists(imap_conn, folder_name="Warmup", account_email="Unknown"):
    """
    Checks if a folder exists using SELECT.
    Notifies admin via Django's send_mail on first creation or error.
    """
    quoted_name = f'"{folder_name}"'
    admin_recipient = "pyabdpy@gmail.com"
    
    def trigger_admin_notif(status_msg, is_error=False):
        try:
            subject = f"{'[ERROR]' if is_error else '[NEW FOLDER]'} Dispatch Skool Warmup: {account_email}"
            message = (
                f"Status Update for {account_email}\n"
                f"Action: Folder Check/Creation\n"
                f"Folder Name: {folder_name}\n"
                f"Detail: {status_msg}"
            )
            
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_recipient],
                fail_silently=False,
            )
        except Exception as e:
            print(f"Critical: Could not send admin notification email: {e}")

    try:
        # 1. Check existence via SELECT
        status, _ = imap_conn.select(quoted_name)
        
        if status == 'OK':
            # Folder exists, no notification needed to keep your inbox clean
            return True

        # 2. Attempt Creation if SELECT fails
        print(f"Warmup folder missing for {account_email}. Creating...")
        create_status, create_resp = imap_conn.create(quoted_name)
        
        if create_status == 'OK':
            # Success - Notify that a new folder was initialized
            trigger_admin_notif(f"Successfully created and initialized the '{folder_name}' folder.")
            
            try:
                imap_conn.subscribe(quoted_name)
            except:
                pass
            return True
        else:
            # Failure - Notify of the IMAP error
            trigger_admin_notif(f"IMAP CREATE failed. Response: {create_resp}", is_error=True)
            return False

    except Exception as e:
        # Exception - Notify of the code-level crash
        trigger_admin_notif(f"Python Exception: {str(e)}", is_error=True)
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
        if not ensure_folder_exists(imap_conn, "Warmup", email_account.email):
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
        if status == 'undeliverable' or score <= 40:
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

