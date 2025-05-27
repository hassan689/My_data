import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection
from users.models import EmailAccount
from django.utils import timezone



email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"  # Make sure this is defined globally

def send_emails_task(email_account_id, leads, subject, body, delay):
    try:

        email_account = EmailAccount.objects.get(id=email_account_id)
        decrypted_password = email_account.get_password()
        
        print(f"[{timezone.now()}] Started task for {email_account.email_address}, {len(leads)} leads")

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

        for lead in leads:
            if not lead.get('email') or not re.match(email_regex, lead['email']):
                print(f"Skipping invalid email: {lead.get('email', 'N/A')}")
                continue

            personalized_subject = subject.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))
            personalized_body = body.replace("[name]", str(lead['name'])).replace("[mc_number]", str(lead['mc_number']))

            try:
                print(f"[{timezone.now()}] Sent email to {lead['email']}")

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
                sent_count += 1
                time.sleep(delay)

            except Exception as e:
                print(f"Failed to send to {lead['email']}: {e}")

        connection.close()
        print(f"{sent_count}/{len(leads)} emails sent using {email_account.email_address}.")

    except Exception as e:
        print(f"Fatal error in send_emails_task: {e}")

    return True

