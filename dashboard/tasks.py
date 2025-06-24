import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection
from users.models import EmailAccount, CustomUser
from unibox.models import EmailThread, OutgoingEmailMessage
from dashboard.models import GmailToken, CampaignRecord
import random
from growth_skool.celery import app
from email.utils import make_msgid
import uuid


email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

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
    use_tls = email_account.server_type == "TLS"
    use_ssl = email_account.server_type == "SSL"

    if use_tls and use_ssl:
        print("Invalid configuration: Cannot enable both TLS and SSL.")
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

# ----------------------------------------------------
# NEW CELERY TASK for processing a chunk of leads
# This task will be consumed by Celery workers
# ----------------------------------------------------
@app.task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(email_account_id, user_id, leads, subject, body, min_delay, max_delay):
    
    try:
        email_account = EmailAccount.objects.get(id=email_account_id)
        print(f"Celery Task Debug: Successfully retrieved EmailAccount: {email_account.email_address}.")
        
        decrypted_password = email_account.get_password()

        sent_count = 0
        mailbox_instance = None

        try:
            mailbox_instance = GmailToken.objects.get(email_account=email_account)
        except: # Not a mailbox instance, bcz not necessary that every account sending out emails will have Gmail API connected (Gmail Token instance)
            pass # the reason might be that this specifc account hasn't been connected yet or it's not even a Gmail Account to begin with

        # Initial connection setup
        connection = get_email_connection(email_account, decrypted_password)

        for lead in leads:
            if not isinstance(lead, dict) or 'email' not in lead:
                print(f"Skipping invalid lead: {lead}")
                continue

            if not re.fullmatch(email_regex, lead['email']):
                print(f"Skipping invalid email format: {lead['email']}")
                continue
            
            if sent_count > 0 and sent_count % 10 == 0:
                print(f"Celery Task: Reconnecting after {sent_count} emails for {email_account.email_address}.")
                try:
                    if connection and connection.is_open(): # Check if connection exists and is open before closing
                        connection.close()
                except Exception as close_e:
                    print(f"Celery Task: Error closing connection before re-opening: {close_e}")
                
                connection = get_email_connection(email_account, decrypted_password)
            
            # Personalize subject and body (only once per lead)
            personalized_subject = personalize_template(subject, lead)
            personalized_body = personalize_template(body, lead)
            message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')

            delay = random.randint(min_delay, max_delay)
            time.sleep(delay) 

            try:
                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=email_account.email_address,
                    to=[lead['email']],
                    connection=connection
                )
                msg.extra_headers = {'Message-ID': message_id}
                msg.attach_alternative(personalized_body, "text/html")
                
                msg.send()
                sent_count += 1

                if mailbox_instance: # Reason explained above
                    
                    thread, created  = EmailThread.objects.get_or_create(
                        mailbox=mailbox_instance, 
                        email1=email_account.email_address,
                        email2=lead['email'],
                        subject=personalized_subject,
                        defaults={
                            'is_read': True,
                        }
                    )

                    OutgoingEmailMessage.objects.create(
                        thread=thread,  # Attach to the new thread
                        subject=personalized_subject,
                        body=personalized_body,
                        recipient=lead['email'],
                        sender=email_account.email_address,
                        message_id=message_id,
                        in_reply_to=None,  # It's not a reply, it's a first message
                    )

            except Exception as e:
                print(f"Celery Task: Failed to send to {lead['email']} (via {email_account.email_address}): {e}")


        sending_user = CustomUser.objects.get(id=user_id)

        # Save the campaign record to db for analysis
        CampaignRecord.objects.create(
            subject = subject,
            body = body,
            launched_by = sending_user,
            sender_account = email_account,
            total_recipients = len(leads),
            sent_count = sent_count
        )

        connection.close()
        print(f"Celery Task: {sent_count}/{len(leads)} emails sent for chunk using {email_account.email_address}.")

    except EmailAccount.DoesNotExist:
        print(f"Celery Task Error: EmailAccount with ID {email_account_id} does not exist.")
    except Exception as e:
        print(f"Celery Task Error: An unexpected error occurred in send_emails_chunk_celery_task: {e}")


# The problem was the Celery worker encountering "please run connect() first" errors, 
# indicating a closed SMTP connection when sending emails to a long list of leads with 
# significant delays between each. The solution implemented involves re-establishing the 
# SMTP connection every 10 emails, closing the old one and opening a new one, to prevent 
# server timeouts and connection instability during prolonged idle periods.
