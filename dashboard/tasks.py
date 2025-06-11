import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection
from users.models import EmailAccount
from django.utils import timezone
import random
from growth_skool.celery import app

email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

def chunk_list(data, chunk_size):
    """Yield successive chunks of given size from the list."""
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


# ----------------------------------------------------
# NEW CELERY TASK for processing a chunk of leads
# This task will be consumed by Celery workers
# ----------------------------------------------------
@app.task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(email_account_id, leads_chunk, subject, body, min_delay, max_delay):
    
    print(f"[{timezone.now()}] Celery Task Debug: Task received. Trying to get EmailAccount ID: {email_account_id}...")
    try:
        email_account = EmailAccount.objects.get(id=email_account_id)
        print(f"[{timezone.now()}] Celery Task Debug: Successfully retrieved EmailAccount: {email_account.email_address}.")
        
        decrypted_password = email_account.get_password()
        print(f"[{timezone.now()}] Celery Task Debug: Successfully decrypted password.")
        
        # Original logic continues from here
        print(f"[{timezone.now()}] Celery Task: Started processing chunk for {email_account.email_address}, {len(leads_chunk)} leads")

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
        print(f"[{timezone.now()}] Celery Task: SMTP Connection opened.") # New debug print
        sent_count = 0

        for lead in leads_chunk:
            if not isinstance(lead, dict) or 'email' not in lead:
                print(f"Skipping invalid lead: {lead}")
                continue

            if not re.fullmatch(email_regex, lead['email']):
                print(f"Skipping invalid email format: {lead['email']}")
                continue
            
            # Personalize subject and body (only once per lead)
            personalized_subject = subject.replace("[name]", str(lead.get('name', ''))).replace("[mc_number]", str(lead.get('mc_number', '')))
            personalized_body = body.replace("[name]", str(lead.get('name', '')).replace("[mc_number]", str(lead.get('mc_number', ''))))

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
                print(f"[{timezone.now()}] Celery Task: Sent email to {lead['email']} (via {email_account.email_address})")


            except Exception as e:
                print(f"[{timezone.now()}] Celery Task: Failed to send to {lead['email']} (via {email_account.email_address}): {e}")

        connection.close()
        print(f"[{timezone.now()}] Celery Task: {sent_count}/{len(leads_chunk)} emails sent for chunk using {email_account.email_address}.")

    except EmailAccount.DoesNotExist:
        print(f"[{timezone.now()}] Celery Task Error: EmailAccount with ID {email_account_id} does not exist.")
    except Exception as e:
        print(f"[{timezone.now()}] Celery Task Error: An unexpected error occurred in send_emails_chunk_celery_task: {e}")


# ----------------------------------------------------
# MODIFIED Django Q Task (Campaign Manager)
# This task will be called by Django Q
# ----------------------------------------------------
def send_emails_task(email_account_id, leads, subject, body, min_delay, max_delay):
    chunk_size = 150 # 150 leads per worker

    try:
        print(f"[{timezone.now()}] Django Q Task: Campaign Manager started for email_account_id: {email_account_id}, total leads: {len(leads)}")

        for i, leads_chunk in enumerate(chunk_list(leads, chunk_size)):
            print(f"[{timezone.now()}] Django Q Task: Submitting chunk {i+1} ({len(leads_chunk)} leads) to Celery...")
            # Call the Celery task here using .delay()
            send_emails_chunk_celery_task.delay(email_account_id, leads_chunk, subject, body, min_delay, max_delay)
            
        print(f"[{timezone.now()}] Django Q Task: All chunks submitted to Celery for email_account_id: {email_account_id}.")

    except Exception as e:
        print(f"[{timezone.now()}] Django Q Task Error: An error occurred in Campaign Manager: {e}")

