import random
import re
import smtplib
from django.core.mail import get_connection
from users.models import EmailAccount


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



