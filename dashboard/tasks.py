# tasks.py
import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils.timezone import now
from users.models import EmailAccount
from django.conf import settings
from django_q.tasks import async_task



email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"  # Make sure this is defined globally

def send_emails_task(email_account_id, leads, subject, body, delay, max_retries=3, attempt=1, max_total_attempts=2):
    try:
        email_account = EmailAccount.objects.get(id=email_account_id)
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

        try:
            connection.open()
        except Exception as e:
            print(f"Exception while opening connection for {email_account.email_address}: {e}")
            return

        sent_count = 0
        failed_leads = []

        for lead in leads:
            if not lead.get('email') or not re.match(email_regex, lead['email']):
                print(f"Skipping invalid email: {lead.get('email', 'N/A')}")
                continue

            personalized_subject = subject.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))
            personalized_body = body.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))

            success = False
            for attempt in range(1, max_retries + 1):
                try:
                    personalized_subject = subject.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))
                    personalized_body = body.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))

                    msg = EmailMultiAlternatives(
                        subject=personalized_subject,
                        body=personalized_body,
                        from_email=email_account.email_address,
                        to=[lead['email']],
                        connection=connection
                    )
                    msg.attach_alternative(personalized_body, "text/html")
                    msg.send()
                    success = True
                    sent_count += 1
                    time.sleep(delay)
                    break  # Stop retrying this lead
                except Exception as e:
                    print(f"[RETRY {attempt}/{max_retries}] Failed to send to {lead['email']}: {e}")
                    time.sleep(2 ** attempt)  # Optional exponential backoff

            if not success:
                failed_leads.append(lead)

        connection.close()
        print(f"[SUMMARY] Attempt {attempt}: {sent_count}/{len(leads)} emails sent using {email_account.email_address}.")
        print(f"[FAILED] {len(failed_leads)} emails failed after {max_retries} retries.")

        email_account.last_used_at = now()
        email_account.save(update_fields=["last_used_at"])

        # === 🔁 Retry failed leads ===
        if failed_leads and attempt < max_total_attempts:
            print(f"[RETRYING FAILED LEADS] Scheduling retry attempt {attempt + 1}")
            async_task(
                'dashboard.tasks.send_emails_task',
                email_account_id,
                failed_leads,
                subject,
                body,
                delay,
                max_retries,
                attempt + 1,
                max_total_attempts
            )
        elif failed_leads:
            print(f"[FAILED PERMANENTLY] {len(failed_leads)} leads could not be sent even after {max_total_attempts} attempts.")

    except Exception as e:
        print(f"Fatal error in send_emails_task: {e}")
