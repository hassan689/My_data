import random
import re
import smtplib
import imaplib
import email
from django.core.mail import get_connection
from users.models import EmailAccount


IMAP_SETTINGS_MAP = {
    'gmail':     {'host': 'imap.gmail.com', 'port': 993},
    'outlook':   {'host': 'outlook.office365.com', 'port': 993},
    'yahoo':     {'host': 'imap.mail.yahoo.com', 'port': 993},
    'zoho':      {'host': 'imap.zoho.com', 'port': 993},
    'hostinger': {'host': 'imap.hostinger.com', 'port': 993},
    'namecheap': {'host': 'imap.privateemail.com', 'port': 993},
}

# A large list of common words to generate unique content on the fly.
WORD_LIST = [
    "hello", "hi", "hey", "greetings", "dear", "thanks", "appreciate",
    "email", "message", "note", "reply", "response", "subject", "body",
    "quick", "fast", "brief", "short", "long", "detailed", "insightful",
    "question", "query", "inquiry", "thought", "idea", "opinion", "perspective",
    "topic", "subject", "matter", "area", "field", "domain", "niche",
    "conversation", "chat", "talk", "discussion", "dialogue", "correspondence",
    "hope", "wish", "expect", "assume", "believe", "know", "think",
    "good", "great", "nice", "excellent", "fine", "well", "okay",
    "day", "week", "month", "year", "time", "moment", "while",
    "you", "i", "we", "they", "he", "she", "it", "this", "that",
    "connect", "reach", "get", "find", "link", "join", "meet",
    "share", "provide", "give", "offer", "send", "receive", "exchange",
    "business", "company", "project", "work", "task", "job", "role",
    "account", "profile", "contact", "person", "individual", "professional",
    "process", "strategy", "approach", "method", "procedure", "plan", "way",
    "follow-up", "circling", "revisiting", "coming back to", "checking in on",
    "something", "anything", "everything", "nothing",
    "about", "regarding", "concerning", "on", "in", "with", "for",
    "from", "to", "at", "by", "like", "as", "than", "so", "but",
    "because", "since", "while", "when", "where", "what", "who", "which",
    "and", "or", "nor", "but", "yet", "so", "for", "nor",
    "can", "could", "will", "would", "should", "must", "might", "may",
    "have", "has", "had", "is", "am", "are", "was", "were", "be",
    "do", "did", "does", "done", "make", "made", "making", "take", "took",
    "look", "see", "find", "get", "go", "went", "going", "come", "came",
    "your", "my", "our", "their", "his", "her", "its",
    "profile", "inbox", "link", "system", "platform", "software",
    "wondering", "curious", "interested", "looking", "thinking", "focused",
    "outreach", "note", "message", "touchpoint",
    "how", "what", "where", "when", "why", "who",
    "specific", "particular", "certain", "exact", "distinct",
    "details", "information", "data", "facts", "figures",
    "best", "better", "greatest", "most", "least", "worst",
    "way", "method", "manner", "fashion", "style",
    "new", "old", "recent", "past", "future", "current",
    "another", "other", "different", "similar",
    "final", "last", "concluding", "ending", "ultimate",
    "a", "an", "the", "some", "any", "no", "all",
]

def generate_gibberish_subject():
    """Generates a random, gibberish subject line."""
    subject_words = random.sample(WORD_LIST, random.randint(3, 6))
    return " ".join([word.capitalize() for word in subject_words])

def generate_gibberish_body(recipient_first_name, sender_company_name):
    """Generates a random, gibberish body paragraph."""
    sentences = []
    num_sentences = random.randint(2, 4)
    for _ in range(num_sentences):
        sentence_words = random.sample(WORD_LIST, random.randint(4, 10))
        # Ensure a capital letter at the start and a period at the end
        sentence = " ".join(sentence_words).capitalize() + "."
        sentences.append(sentence)

    # Inject personalization in a random sentence
    personalization_phrase = f"Hello {recipient_first_name}, I saw your company {sender_company_name} was doing well."
    sentences.insert(random.randint(0, len(sentences)), personalization_phrase)
    
    return " ".join(sentences)

def refresh_targets(campaign):
    
    # Step 1: Get sender and its user
    sender_account = campaign.sender_account
    sender_user = sender_account.user

    # Step 2: Get all email account instances that are eligible for warmup, excluding sender
    eligible_email_accounts = EmailAccount.objects.filter(black_list=False, is_warmup_target=True).exclude(user=sender_user)

    # Step 3: Randomly pick up to 5
    selected_accounts = random.sample(list(eligible_email_accounts), min(5, eligible_email_accounts.count()))

    if selected_accounts:
        campaign.target_accounts.set(selected_accounts)


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


def check_inbox_and_rescue(email_account, target_message_id):
    """
    1. Connects via IMAP.
    2. Searches INBOX for the target_message_id.
    3. If not found, searches SPAM/JUNK folders.
    4. If found in SPAM, moves it to INBOX.
    5. Returns the email body (text) for quoting, or None if not found.
    """
    imap_conn = get_warmup_imap_connection(email_account)
    if not imap_conn:
        return None

    try:
        # 1. Search INBOX
        imap_conn.select("INBOX")
        # Search for Header Message-ID (RFC 822)
        status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
        
        email_ids = messages[0].split()
        
        # 2. If not in Inbox, search Spam
        if not email_ids:
            spam_folders = ["Spam", "Junk", "Junk Email", "[Gmail]/Spam", "Bulk"]
            found_in_spam = False
            
            for folder in spam_folders:
                try:
                    status, _ = imap_conn.select(folder)
                    if status != 'OK':
                        continue
                        
                    status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
                    email_ids = messages[0].split()
                    
                    if email_ids:
                        print(f"Found Message-ID {target_message_id} in {folder}. Moving to INBOX.")
                        # Move to Inbox
                        msg_num = email_ids[0]
                        # Copy to Inbox
                        copy_res = imap_conn.copy(msg_num, "INBOX")
                        if copy_res[0] == 'OK':
                            # Mark as Deleted in Spam so it's effectively a "Move"
                            imap_conn.store(msg_num, '+FLAGS', '\\Deleted')
                            imap_conn.expunge()
                            found_in_spam = True
                            
                            # Switch back to Inbox to fetch the content
                            imap_conn.select("INBOX")
                            # Search again in Inbox to get the new ID
                            status, messages = imap_conn.search(None, f'(HEADER Message-ID "{target_message_id}")')
                            email_ids = messages[0].split()
                        break
                except Exception as e:
                    continue
            
            if not email_ids:
                return None # Truly lost

        # 3. Fetch Content (for Quoting)
        latest_email_id = email_ids[-1]
        status, msg_data = imap_conn.fetch(latest_email_id, "(RFC822)")
        
        if status != 'OK' or not msg_data or not msg_data[0] or len(msg_data[0]) < 2:
            return None

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        # Extract plain text body
        body_content = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = part.get("Content-Disposition")
                if content_type == "text/plain" and "attachment" not in str(content_disposition):
                    try:
                        body_content = part.get_payload(decode=True).decode()
                    except:
                        pass
                    break # Found the text part
        else:
            try:
                body_content = msg.get_payload(decode=True).decode()
            except:
                pass

        return body_content

    except Exception as e:
        print(f"Error in check_inbox_and_rescue: {e}")
        return None
    finally:
        try:
            imap_conn.close()
            imap_conn.logout()
        except:
            pass

