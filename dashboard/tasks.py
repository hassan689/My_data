import time
import re
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail, EmailMessage
from users.models import EmailAccount, CustomUser
from unibox.models import EmailThread, OutgoingEmailMessage
from dashboard.models import GmailToken, CampaignRecord, EmailOpen
import random
from growth_skool.celery import app
from django.utils import timezone
from email.utils import make_msgid
import uuid
from django.conf import settings
from django_celery_results.models import TaskResult
from django.utils.timezone import now, timedelta
from django.urls import reverse
from urllib.parse import urljoin
from django.utils.encoding import force_str
from django.db import connections


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


@app.task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(email_account_id, leads, subject, body, min_delay, max_delay, campaign_record_id):

    print(f"Celery Task Debug: Starting send_emails_chunk_celery_task with EmailAccount ID {email_account_id} and {len(leads)} leads.")
    
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

        campaign = CampaignRecord.objects.get(id=campaign_record_id)

        campaign.total_recipients = len(leads) # Update total_recipients based on how many leads are left to send, if it was resumed
        campaign.status = 'processing'
        campaign.sent_count = sent_count
        campaign.save(update_fields=['status', 'total_recipients', 'sent_count'])
        stopped = False
        sent_leads_batch = []  # track last 5 leads

        for i, lead in enumerate(leads, 1):

            if i % 5 == 0:
                campaign.refresh_from_db()
                if campaign.status == 'cancelled':
                    print("🛑 Campaign was cancelled. Exiting task.")
                    stopped = True
                    break
                else:
                    # Remove all the sent leads in the batch from campaign.leads_data
                    if campaign.leads_data:
                        campaign.leads_data = [
                            l for l in campaign.leads_data
                            if l not in sent_leads_batch
                        ]
                    campaign.sent_count = sent_count
                    campaign.save(update_fields=['sent_count', 'leads_data'])
                    sent_leads_batch = [] # Clear the batch


            if not isinstance(lead, dict) or 'Email' not in lead:
                print(f"Skipping invalid lead: {lead}")
                continue

            if not re.fullmatch(email_regex, lead['Email']):
                print(f"Skipping invalid email format: {lead['Email']}")
                continue
            
            if sent_count > 0 and sent_count % 10 == 0:
                try:
                    connection.close()
                except Exception:
                    pass
                connection = get_email_connection(email_account, decrypted_password)
            
            # Personalize subject and body (only once per lead)
            personalized_subject = personalize_template(subject, lead)
            personalized_body = personalize_template(body, lead)
            message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')

            delay = random.randint(min_delay, max_delay)

            # 🚨 CRITICAL FIX: Close connections before sleeping
            connections.close_all()
            
            time.sleep(delay)

            if campaign.track_campaign:

                unique_id = uuid.uuid4()
                pixel_url = reverse('dashboard:track_open', kwargs={'unique_identifier': unique_id})
                pixel_link = urljoin(settings.BASE_URL, pixel_url)

                try:
                    email_log = EmailOpen.objects.create(
                        campaign = campaign,
                        recipient_email = lead['Email'],
                        unique_identifier = unique_id,
                        mc_number = lead.get('MC Number', ''),
                        legal_name = lead.get('Legal Name', '')
                    )
                except Exception as e:
                    print(f"Exception dring email log entry: {e}")

                tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
                personalized_body += tracking_pixel

                print("\nTracking campaign\n")

            try:
                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=email_account.email_address,
                    to=[lead['Email']],
                    connection=connection
                )
                msg.extra_headers = {'Message-ID': message_id}
                msg.attach_alternative(personalized_body, "text/html")
                
                try:
                    msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower():
                        print("SMTP connection lost, reconnecting...")
                        connection = get_email_connection(email_account, decrypted_password)
                        msg.connection = connection
                        msg.send()
                    else:
                        raise e

                sent_count += 1
                sent_leads_batch.append(lead)

                print(f"Celery Task: Sent to {lead['Email']} (via {email_account.email_address}).")

                if mailbox_instance: # Reason explained above
                    
                    thread, created  = EmailThread.objects.get_or_create(
                        mailbox=mailbox_instance, 
                        email1=email_account.email_address,
                        email2=lead['Email'],
                        subject=personalized_subject,
                        defaults={
                            'is_read': True,
                        }
                    )

                    OutgoingEmailMessage.objects.create(
                        thread=thread,  # Attach to the new thread
                        subject=personalized_subject,
                        body=personalized_body,
                        recipient=lead['Email'],
                        sender=email_account.email_address,
                        message_id=message_id,
                        in_reply_to=None,  # It's not a reply, it's a first message
                    )

            except Exception as e:
                print(f"Celery Task: Failed to send to {lead['Email']} (via {email_account.email_address}): {e}")

                error_message = str(e)
                if "Daily user sending limit exceeded" in error_message:
                    email_limit_exceeded = True
                    
                    # Send notification email to the affected account
                    notification_subject = f"⚠️ Campaign Halted: Daily Sending Limit Exceeded for {email_account.email_address}"
                    notification_body = (
                        f"Dear user,\n\n"
                        f"Your email campaign using the account '{email_account.email_address}' has been halted "
                        f"because **Gmail has indicated that the daily sending limit for this email account has been exceeded.**\n\n"
                        f"**This limit is imposed by Gmail, not by DispatchSkool.**\n\n"
                        f"For more information on Gmail sending limits, please visit: "
                        f"https://support.google.com/a/answer/166852\n\n"
                        f"Please wait 24 hours before trying to send new campaigns from this account.\n\n"
                        f"Regards,\nThe DispatchSkool Team"
                    )
                    
                    try:
                        send_mail(
                            notification_subject,
                            notification_body,
                            settings.EMAIL_HOST_USER, # Sender: Your system's configured email host user
                            [email_account.email_address], # Recipient: The email account that hit the limit
                            fail_silently=False,
                        )
                        print(f"Celery Task: Sent daily limit exceeded notification to {email_account.email_address}")
                    except Exception as notify_e:
                        print(f"Celery Task: Failed to send limit exceeded notification: {notify_e}")
                    
                    # Stop processing this chunk for the current email_account
                    break # This will break the loop and halt the campaign, freeing the celry worker

                elif "timeout exceeded" in str(error_message).lower() or "timed out" in str(error_message).lower() or "Connection unexpectedly closed" in str(error_message).lower():
                    
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = get_email_connection(email_account, decrypted_password)

                    try:
                        msg.send()
                        sent_count += 1
                    except Exception as e:
                        if "please run connect() first" in str(e).lower():
                            print("SMTP connection lost, reconnecting...")
                            connection = get_email_connection(email_account, decrypted_password)
                            msg.connection = connection
                            msg.send()
                            sent_count += 1
                        else:
                            raise e

        # Update the status of the above created new_campaign after finishing the loop and sending all the mails

        if not stopped:
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])

        # Final cleanup for any remaining leads in batch
        if sent_leads_batch:
            campaign.leads_data = [
                l for l in campaign.leads_data
                if l not in sent_leads_batch
            ]
            campaign.save(update_fields=['leads_data'])

        campaign.sent_count = sent_count
        campaign.save(update_fields=['sent_count'])

        connection.close()
        print(f"Celery Task: {sent_count}/{len(leads)} emails sent for chunk using {email_account.email_address}.")

    except EmailAccount.DoesNotExist:
        print(f"Celery Task Error: EmailAccount with ID {email_account_id} does not exist.")
    except Exception as e:
        print(f"Celery Task Error: An unexpected error occurred in send_emails_chunk_celery_task: {e}")



@app.task(name="dashboard.tasks.launch_scheduled_campaign_checker")
def launch_scheduled_campaign_checker():
    
    # Get the current time in UTC, as all scheduled_launch_time are stored in UTC
    now_utc = timezone.now()

    # Find pending campaigns that are due to be launched
    campaigns_to_launch = CampaignRecord.objects.filter(
        status='pending',
        scheduled_launch_time__lte=now_utc # Campaigns whose scheduled time is now or in the past (UTC)
    ).select_related('sender_account', 'launched_by') # Optimize query by prefetching related objects

    if not campaigns_to_launch.exists():
        print("No scheduled campaigns found to launch.")
        return

    print(f"Found {campaigns_to_launch.count()} campaigns to launch.")
    for campaign_record in campaigns_to_launch:
        try:

            # Retrieve leads data from JSONField
            leads = campaign_record.leads_data

            # Trigger the actual email sending task
            send_emails_chunk_celery_task.delay(
                campaign_record.sender_account.id,
                leads,
                campaign_record.subject,
                campaign_record.body,
                campaign_record.min_delay,
                campaign_record.max_delay,
                campaign_record.id
            )
            print(f"Triggered send_emails_chunk_celery_task for CampaignRecord {campaign_record.id}.")

            campaign_record.sender_account.last_used_at = now_utc
            campaign_record.sender_account.save(update_fields=["last_used_at"])
            campaign_record.status = 'processing'
            campaign_record.save(update_fields=['status'])

        except Exception as e:
            # Log any errors that occur during the launching process
            print(f"Error launching scheduled campaign {campaign_record.id}: {e}")
            # Optionally, set status to 'failed' if an error prevents launching
            campaign_record.status = 'failed'
            campaign_record.save(update_fields=['status'])



@app.task(name="dashboard.tasks.send_account_attach_notif_email")
def send_account_attach_notif_email(email_account_id, user_id):
    
    print(f"Sending account attach notification email for EmailAccount ID {email_account_id} to User ID {user_id}")
    try:
        email_account = EmailAccount.objects.get(id=email_account_id)
        decrypted_password = email_account.get_password()
        user = CustomUser.objects.get(id=user_id)

        # Determine the SMTP security type
        use_tls = email_account.server_type == "STARTTLS" or email_account.server_type == "TLS"
        use_ssl = email_account.server_type == "SSL"

        if use_tls and use_ssl:
            print("Invalid configuration: Cannot enable all TLS, SSL and STARTTLS.")
            return

        # Correct credentials entered
        try:
            # Create SMTP connection
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

            # Email content
            subject = "Email account configured successfully"
            body = (
                f"Hello {user.first_name},\n\n"
                f"This is to notify you that your email account {email_account.email_address} "
                "has been successfully configured with Dispatch Skool and is now ready to launch campaigns.\n\n"
                "Best Regards,\nThe Dispatch Skool Team."
            )
            from_email = email_account.email_address
            recipient_list = [user.email]

            body_encoded = force_str(body, 'utf-8', errors='replace')

            # Create and send email
            email_message = EmailMessage(
                subject, body_encoded, from_email, recipient_list, connection=connection
            )
            email_message.send()
            connection.close()

        # Incorrect credentials entered
        except Exception as e:
            subject = "Email account configuration failure"
            body = (
                f"Hello {user.first_name},\n\n"
                f"Error during email attach: {e}\n\n"
                f"This is to notify you that your email account {email_account.email_address} "
                "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                "In case of any problems, feel free to reach out.\n\n"
                "Best Regards,\nThe Dispatch Skool Team."
            )
            from_email = settings.EMAIL_HOST_USER
            recipient_list = [user.email]

            body_encoded = force_str(body, 'utf-8', errors='replace')

            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )

    except Exception as e:
        print(f"Error sending notification email: {e}")



@app.task(name="dashboard.tasks.clear_successful_task_results")
def clear_successful_task_results():
    # Delete only successful tasks older than 1 day (optional safety filter)
    one_day_ago = now() - timedelta(days=1)
    deleted, _ = TaskResult.objects.filter(
        status="SUCCESS",
        date_done__lt=one_day_ago
    ).delete()
    return f"Deleted {deleted} successful task results"



@app.task(name="dashboard.tasks.check_processing_campaign_count")
def check_processing_campaign_count():
    """
    Checks the number of processing campaigns and sends an alert if the threshold is met.
    """
    PROCESSING_CAMPAIGN_THRESHOLD = 110
    
    processing_count = CampaignRecord.objects.filter(status='processing').count()

    if processing_count >= PROCESSING_CAMPAIGN_THRESHOLD:
        subject = f"⚠️ Alert: High Number of Processing Campaigns ({processing_count})"
        body = (
            f"Hello,\n\n"
            f"This is an automated alert. The number of active campaigns with 'processing' status has reached {processing_count}.\n"
            f"This is approaching the Celery worker limit of 120.\n\n"
            f"Please check the server load and campaign queue.\n\n"
            f"Regards,\nThe DispatchSkool Team"
        )
        
        # Replace with your email address
        recipient_list = ['abdullahatif132@gmail.com'] 
        
        try:
            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                recipient_list,
                fail_silently=False,
            )
        except Exception as e:
            print(f"Failed to send processing campaign count alert email: {e}")



@app.task(name="dashboard.tasks.cleanup_email_opens")
def cleanup_email_opens():
    """
    Deletes EmailOpen entries where:
    - timestamp older than 30 days
    """
    cutoff_date = timezone.now() - timedelta(days=30)
    deleted_count, _ = EmailOpen.objects.filter(
        timestamp__lt=cutoff_date
    ).delete()

