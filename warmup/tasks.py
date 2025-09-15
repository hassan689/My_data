import random
import re
from django.utils import timezone
from .models import WarmupCampaign, WarmupTemplateSet, WarmupMessage
from users.models import EmailAccount
from growth_skool.celery import app
from django.core.mail import get_connection, EmailMultiAlternatives, send_mail, EmailMessage
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_str
import time


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
    """Refreshes the target accounts for a campaign."""
    # Select new random targets
    new_target_accounts = list(EmailAccount.objects.filter(
        is_warmup_target=True
    ).exclude(
        id=campaign.sender_account.id
    ).exclude(
        black_list=True
    ).order_by('?')[:5])

    if new_target_accounts:
        campaign.target_accounts.set(new_target_accounts)
        print(f"Refreshed target accounts for campaign {campaign.id}.")
    else:
        print("Not enough warmup target accounts available for refresh.")


@app.task(name="warmup.tasks.activate_warmup_campaign")
def activate_warmup_campaign(sender_account_id, template_set_id):
    
    sender_account = EmailAccount.objects.get(id=sender_account_id)
    template_set = WarmupTemplateSet.objects.get(id=template_set_id)

    # Select 5 random targets
    target_accounts = list(EmailAccount.objects.filter(
        is_warmup_target=True
    ).exclude(
        id=sender_account_id
    ).exclude(
        black_list=True # Accounts known for causing trouble
    ).order_by('?')[:5])
    
    if not target_accounts:
        print("Not enough warmup target accounts available.")
        return

    # Look for existing campaign; if it exists, update it. If not, create a new one.
    try:
        campaign = WarmupCampaign.objects.get(sender_account=sender_account)
        campaign.status = 'Active'
        campaign.last_action_at = timezone.now()
        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(0, 3))
        campaign.save()

    except WarmupCampaign.DoesNotExist:
        # Create a new WarmupCampaign instance
        campaign = WarmupCampaign.objects.create(
            sender_account=sender_account,
            template_set=template_set,
            status='Active',
            last_action_at=timezone.now(),
            next_action_at=timezone.now() + timedelta(hours=random.uniform(24, 36)) # Random delay 24-36 hours
        )

    campaign.target_accounts.set(target_accounts)

    # Trigger the first step of the conversation
    send_warmup_step.delay(campaign.id, step_number=0)



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

    if use_tls and use_ssl:
        print("Invalid configuration: Cannot enable all TLS, SSL and STARTTLS.")
        return

    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=email_account.host,
        port=email_account.port_number,
        username=email_account.email_address,
        password=decrypted_password,
        use_tls=use_tls,
        use_ssl=use_ssl,
    )
    connection.open()
    return connection


@app.task(name="warmup.tasks.send_warmup_step")
def send_warmup_step(campaign_id, step_number):
    """
    Sends the next step of a warmup conversation for a given campaign.
    Even steps (0, 2, 4...) are for the sender, odd steps (1, 3, 5...) are for the targets.
    """
    try:
        campaign = WarmupCampaign.objects.select_related(
            'sender_account', 'template_set'
        ).prefetch_related(
            'target_accounts'
        ).get(id=campaign_id)
    except WarmupCampaign.DoesNotExist:
        print(f"Warmup campaign with ID {campaign_id} not found.")
        return

    # Check if the campaign has already completed or failed
    if campaign.status in ['Complete', 'Failed']:
        print(f"Campaign {campaign.id} is already in {campaign.status} state. Skipping.")
        return
    
    # NEW LOGIC: Refresh targets after a full cycle (e.g., every 2 steps)
    if step_number > 0 and step_number % 2 == 0:
        refresh_targets(campaign)
    
    # Sender's turn (Even steps: 0, 2, 4...)
    if step_number % 2 == 0:
        try:
            sender_account = campaign.sender_account
            recipients = list(campaign.target_accounts.all())

            # Establish connection for the sender account
            decrypted_password = sender_account.get_password()
            connection = get_email_connection(sender_account, decrypted_password)
            
            def clean_text(text: str) -> str:
                    return text.replace('\xa0', ' ').encode('utf-8', 'ignore').decode('utf-8')
            
            for recipient_account in recipients:

                # NEW LOGIC: Generate subject and body dynamically
                personalized_subject = generate_gibberish_subject()
                personalized_body = generate_gibberish_body(clean_text(recipient_account.user.first_name), clean_text(getattr(sender_account.user, "company_name", "ABC Transports LLC")))
                
                main_msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=sender_account.email_address,
                    to=[recipient_account.email_address],
                    connection=connection
                )
                main_msg.encoding = 'utf-8'
                try:
                    main_msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower() or "Connection unexpectedly closed" in str(e).lower() or "Connection reset by peer" in str(e).lower():
                        connection = get_email_connection(sender_account, decrypted_password)
                        main_msg.connection = connection
                        main_msg.send()
                    else:
                        raise e

                WarmupMessage.objects.create(
                    campaign=campaign,
                    sender=sender_account,
                    recipient=recipient_account,
                    subject=personalized_subject,
                    body=personalized_body
                )

                time.sleep(random.randint(30, 60))

            if connection:
                connection.close()

        except Exception as e:
            
            if "Username and Password not accepted" in str(e):

                campaign.status = 'Failed'
                campaign.save(update_fields=['status'])

                sender_account.is_warmup_target = False # the account is not attached properly and will only be a pain to keep attempting the  warmup
                sender_account.save(update_fields=['is_warmup_target'])

                subject = "Email account configuration failure"
                body = (
                    f"Hello {sender_account.user.first_name},\n\n"
                    f"Error during email attach: {e}\n\n"
                    f"This is to notify you that your email account {sender_account.email_address} "
                    "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                    "In case of any problems, feel free to reach out.\n\n"
                    "Best Regards,\nThe Dispatch Skool Team."
                )
                from_email = settings.EMAIL_HOST_USER
                recipient_list = [sender_account.user.email]

                body_encoded = force_str(body, 'utf-8', errors='replace')

                email_message = EmailMessage(
                    subject,
                    body_encoded,
                    from_email,
                    recipient_list
                )
                email_message.send()
            
            elif "Daily user sending limit exceeded" in str(e):
                
                # This just means that campaign can't be sent today, so it'll just be set to a later date
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                campaign.save(update_fields=['next_action_at'])

            elif "codec can't encode character" in str(e): # '\xa0' error
                
                personalized_subject = generate_gibberish_subject()
                personalized_body = generate_gibberish_body(clean_text(recipient_account.user.first_name), clean_text(getattr(sender_account.user, "company_name", "ABC Transports LLC")))
                
                main_msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=sender_account.email_address,
                    to=[recipient_account.email_address],
                    connection=connection
                )
                main_msg.encoding = 'utf-8'
                try:
                    main_msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower() or "Connection unexpectedly closed" in str(e).lower() or "Connection reset by peer" in str(e).lower():
                        connection = get_email_connection(sender_account, decrypted_password)
                        main_msg.connection = connection
                        main_msg.send()
                    else: # retry later
                        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                        campaign.save(update_fields=['next_action_at'])

            elif "Connection unexpectedly closed" in str(e) or "Connection timed out" in str(e) or "Server busy" in str(e) or "Server not connected" in str(e) or "timeout exceeded" in str(e):
                
                connection = get_email_connection(sender_account, decrypted_password)
                try:
                    main_msg.connection = connection
                    main_msg.send()
                except: # retry on a later date
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                    campaign.save(update_fields=['next_action_at'])

            elif "Temporary System Problem" in str(e) or "Concurrent connections limit exceeded" in str(e):
                
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                campaign.save(update_fields=['next_action_at'])

            else: # if something other than the currently known errors, then tell me those
                subject = f"Error during Sender's Turn for Warmup Campaign"
                body = f"Error during sender's turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                recipient_list = ['abdullahatif132@gmail.com']
                send_mail(
                    subject,
                    body,
                    settings.DEFAULT_FROM_EMAIL,
                    recipient_list,
                    fail_silently=False,
                )
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                campaign.save(update_fields=['next_action_at'])

            return
    
    # Targets' turn (Odd steps: 1, 3, 5...)
    else:
        sender_accounts = list(campaign.target_accounts.all())
        recipient_account = campaign.sender_account
        
        for sender_account in sender_accounts:
            
            try: # try for every account and handle thier individual errors accordingly without halting the campaign as much as possible
                decrypted_password = sender_account.get_password()
                connection = get_email_connection(sender_account, decrypted_password)

                def clean_text(text: str) -> str:
                    return text.replace('\xa0', ' ').encode('utf-8', 'ignore').decode('utf-8')
                
                personalized_subject = generate_gibberish_subject()
                personalized_body = generate_gibberish_body(clean_text(recipient_account.user.first_name), clean_text(getattr(sender_account.user, "company_name", "ABC Transports LLC")))
                
                main_msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=sender_account.email_address,
                    to=[recipient_account.email_address],
                    connection=connection
                )
                main_msg.encoding = 'utf-8'
                try:
                    main_msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower() or "Connection unexpectedly closed" in str(e).lower() or "Connection reset by peer" in str(e).lower() or "Disabled by user from hPanel" in str(e).lower():
                        print("SMTP connection lost, reconnecting...")
                        connection = get_email_connection(sender_account, decrypted_password)
                        main_msg.connection = connection
                        main_msg.send()
                    else:
                        raise e
                    
                time.sleep(random.randint(30, 600))

                WarmupMessage.objects.create(
                    campaign=campaign,
                    sender=sender_account,
                    recipient=recipient_account,
                    subject=personalized_subject,
                    body=personalized_body,
                )

                if connection:
                    connection.close()

            except Exception as e:
                
                if "Connection reset by peer" in str(e) or "Disabled by user from hPanel" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection timed out" in str(e):
            
                    connection = get_email_connection(sender_account, decrypted_password)
                    try:
                        main_msg.connection = connection
                        main_msg.send()
                    except:
                        continue

                elif "codec can't encode character" in str(e): # '\xa0' error
                    continue
                
                elif "Please log in with your web browser" in str(e): # These accounts will cause trouble for others as well
                    EmailAccount.objects.filter(email_address=sender_account.email_address).update(is_warmup_target=False, black_list=True)
                    continue
                    
                elif "Daily user sending limit exceeded" in str(e):
                
                    # Using continue bcz, there might be only some whose daily limit is reached and not all, so those accounts will simple be skipped
                    continue

                elif "Username and Password not accepted" in str(e):

                    sender_account.is_warmup_target = False # the account is not attached properly and will only be a pain to keep attempting the  warmup
                    sender_account.save(update_fields=['is_warmup_target'])

                    subject = "Email account configuration failure"
                    body = (
                        f"Hello {sender_account.user.first_name},\n\n"
                        f"Error during email attach: {e}\n\n"
                        f"This is to notify you that your email account {sender_account.email_address} "
                        "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                        "In case of any problems, feel free to reach out.\n\n"
                        "Best Regards,\nThe Dispatch Skool Team."
                    )
                    from_email = settings.EMAIL_HOST_USER
                    recipient_list = [sender_account.user.email]

                    body_encoded = force_str(body, 'utf-8', errors='replace')

                    email_message = EmailMessage(
                        subject,
                        body_encoded,
                        from_email,
                        recipient_list
                    )
                    email_message.send()

                    continue

                else: # if something other than the currently known errors, then tell me those
                    subject = f"Error during Targets' Turn for Warmup Campaign"
                    body = f"Error during targets' turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                    recipient_list = ['abdullahatif132@gmail.com']
                    send_mail(
                        subject,
                        body,
                        settings.DEFAULT_FROM_EMAIL,
                        recipient_list,
                        fail_silently=False,
                    )
                    continue

    # Update campaign status for the next step (only runs if no errors occurred)
    campaign.current_step += 1
    campaign.last_action_at = timezone.now()
    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
    if campaign.current_step >= 10:
        campaign.status = 'Complete'
    campaign.save(update_fields=['current_step', 'last_action_at', 'next_action_at', 'status'])
    
    print(f"Warmup campaign {campaign.id} processed step {step_number} and is now at step {campaign.current_step}.")


@app.task(name="warmup.tasks.process_warmup_convo_beats")
def process_warmup_convo_beats():
    """
    Celery Beat task that checks for active warmup campaigns due for their next step.
    """
    now_utc = timezone.now()
    
    # Find active campaigns where next_action_at is due or last_action_at is null (initial step)
    campaigns_to_process = WarmupCampaign.objects.filter(
        status='Active',
        next_action_at__lte=now_utc
    ).select_related('sender_account', 'template_set')  # Optimize the query

    if not campaigns_to_process.exists():
        print("No warmup campaigns found to process.")
        return

    print(f"\nFound {campaigns_to_process.count()} warmup campaigns to process.\n")
    for campaign in campaigns_to_process:
        try:
            # Trigger the send_warmup_step task for the current step
            send_warmup_step.delay(campaign.id, campaign.current_step)
            print(f"\nTriggered send_warmup_step for WarmupCampaign {campaign.id}, Step {campaign.current_step}.\n")
            
        except Exception as e:
            # Log any errors that occur during the launching process
            print(f"Error triggering send_warmup_step for Campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])


# beat task to clear out warmup messages older than 7 days
@app.task(name="warmup.tasks.clear_old_warmup_messages")
def clear_old_warmup_messages():
    cutoff_date = timezone.now() - timedelta(days=7)
    WarmupMessage.objects.filter(created_at__lt=cutoff_date).delete()


