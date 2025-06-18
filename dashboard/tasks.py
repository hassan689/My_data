import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection
from users.models import EmailAccount
from django.utils import timezone
import random
from growth_skool.celery import app

email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


def personalize_template(template, lead):
    
    # Find all placeholders like [Some Column]
    placeholders = re.findall(r'\[([^\]]+)\]', template)
    
    for ph in placeholders:
        value = str(lead.get(ph, ph))  # Use the column name as fallback if missing
        template = template.replace(f"[{ph}]", value)
    
    return template


# ----------------------------------------------------
# NEW CELERY TASK for processing a chunk of leads
# This task will be consumed by Celery workers
# ----------------------------------------------------
@app.task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(email_account_id, leads, subject, body, min_delay, max_delay):
    
    try:
        email_account = EmailAccount.objects.get(id=email_account_id)
        print(f"[{timezone.now()}] Celery Task Debug: Successfully retrieved EmailAccount: {email_account.email_address}.")
        
        decrypted_password = email_account.get_password()

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
        sent_count = 0

        for lead in leads:
            if not isinstance(lead, dict) or 'email' not in lead:
                print(f"Skipping invalid lead: {lead}")
                continue

            if not re.fullmatch(email_regex, lead['email']):
                print(f"Skipping invalid email format: {lead['email']}")
                continue
            
            # Personalize subject and body (only once per lead)
            personalized_subject = personalize_template(subject, lead)
            personalized_body = personalize_template(body, lead)

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
                msg.attach_alternative(personalized_body, "text/html")
                
                msg.send()
                sent_count += 1

            except Exception as e:
                print(f"[{timezone.now()}] Celery Task: Failed to send to {lead['email']} (via {email_account.email_address}): {e}")

        connection.close()
        print(f"[{timezone.now()}] Celery Task: {sent_count}/{len(leads)} emails sent for chunk using {email_account.email_address}.")

    except EmailAccount.DoesNotExist:
        print(f"[{timezone.now()}] Celery Task Error: EmailAccount with ID {email_account_id} does not exist.")
    except Exception as e:
        print(f"[{timezone.now()}] Celery Task Error: An unexpected error occurred in send_emails_chunk_celery_task: {e}")

