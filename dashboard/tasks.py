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
from django.utils.timezone import timedelta
from django.urls import reverse
from urllib.parse import urljoin
from django.utils.encoding import force_str
from django.db.models import F
from django.db import transaction
from bs4 import BeautifulSoup
from celery import shared_task

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


def sanitize_email_html(html_content, base_url, max_email_width=600):
    """
    1. Converts relative image URLs to absolute URLs.
    2. Keeps the exact pixel width/height set by CKEditor on the <img> tag.
    """
    # Assuming BeautifulSoup is imported correctly
    soup = BeautifulSoup(html_content, 'html.parser')

    # 1. Fix Images and enforce original dimensions
    for img in soup.find_all('img'):
        src = img.get('src')
        
        # Preserve original dimensions from the <img> tag
        original_width = img.get('width')
        original_height = img.get('height')

        if src and src.startswith('/media/'):
            # Make URL absolute
            img['src'] = base_url + src

        # --- REVISED LOGIC STARTS HERE ---
        
        # Get the parent <figure> tag (CKEditor puts width:XX% here)
        figure = img.find_parent('figure')
        
        # 1. Check for CKEditor percentage width on the <figure>
        # This part is now primarily for removing the <figure> style, but 
        # it *can* still calculate the pixel width if needed.
        intended_width_px = None
        if figure and 'style' in figure.attrs:
            style = figure['style']
            match = re.search(r'width:(\d+\.?\d*)%', style)
            
            # If a percentage is found, calculate the intended width (optional, 
            # but good for robust handling).
            if match:
                percentage = float(match.group(1))
                intended_width_px = round(percentage / 100 * max_email_width)
            
            # Always remove the unreliable style from the <figure> tag
            figure.attrs.pop('style', None)

        # 2. **Apply the intended/original width.**
        # Prioritize the width directly on the <img> tag (what CKEditor saved)
        # If CKEditor saved a pixel width, use it. If not, use the max width.
        
        if original_width and original_width.isdigit():
            # Use the exact width saved by CKEditor (e.g., 568 or 305)
            img['width'] = original_width
        elif intended_width_px:
            # Fallback to calculated pixel width from a percentage
            img['width'] = str(intended_width_px)
        else:
            # Fallback to full email width
            img['width'] = str(max_email_width)
            
        # Also re-apply the original height if it was present, or use 'auto'
        if original_height and original_height.isdigit():
            img['height'] = original_height
        else:
            img['height'] = "auto"
            
        # Always remove other unreliable styles from the img tag
        if 'style' in img.attrs:
            del img.attrs['style']
        
        # --- REVISED LOGIC ENDS HERE ---

    # 3. Fix other relative hrefs
    for tag in soup.find_all(href=True):
        href = tag.get('href')
        if href and href.startswith('/media/'):
            tag['href'] = base_url + href
            
    return str(soup)


@shared_task(name="dashboard.send_single_email", acks_late=True, bind=True, default_retry_delay=300) # 5 min retry
def send_single_email(self, campaign_record_id):
    """
    This is the self-perpetuating "Worker" task.
    It processes ONE lead, saves progress, and reschedules itself.
    - acks_late=True: Prevents task loss if the worker crashes mid-send.
    - bind=True: Allows us to call self.retry() for network errors.
    """
    connection = None
    try:
        # 1. --- Get Campaign ---
        campaign = CampaignRecord.objects.get(id=campaign_record_id)

        if campaign.status in ('cancelled', 'launched', 'failed'):
            print(f"Campaign {campaign.id} is finished or cancelled. Stopping chain.")
            return

        if not campaign.leads_data:
            print(f"Campaign {campaign.id} has no leads left. Finishing.")
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
            return

        lead = campaign.leads_data[0] # Get the first lead
        
        # 4. --- Lead Validation ---
        if not isinstance(lead, dict) or 'Email' not in lead:
            print(f"Skipping invalid lead: {lead}. Removing from queue.")
            # Pop and save, then reschedule for the next lead immediately
            campaign.leads_data.pop(0)
            campaign.save(update_fields=['leads_data'])
            send_single_email.apply_async(args=[campaign.id], countdown=1)
            return

        if not re.fullmatch(email_regex, lead['Email']):
            print(f"Skipping invalid email format: {lead['Email']}. Removing from queue.")
            # Pop and save, then reschedule for the next lead immediately
            campaign.leads_data.pop(0)
            campaign.save(update_fields=['leads_data'])
            send_single_email.apply_async(args=[campaign.id], countdown=1)
            return
            
        # 5. --- Get Account & Connection ---
        email_account = campaign.sender_account
        decrypted_password = email_account.get_password()
        connection = get_email_connection(email_account, decrypted_password)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()

        # 6. --- Prepare Email ---
        personalized_subject = personalize_template(campaign.subject, lead)
        personalized_body = personalize_template(campaign.body, lead)
        message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
        DOMAIN = "https://dispatchskool.com"
        personalized_body = sanitize_email_html(personalized_body, DOMAIN)

        if campaign.track_campaign:
            unique_id = uuid.uuid4()
            pixel_url = reverse('dashboard:track_open', kwargs={'unique_identifier': unique_id})
            pixel_link = urljoin(settings.BASE_URL, pixel_url)
            
            try:
                EmailOpen.objects.create(
                    campaign=campaign,
                    recipient_email=lead['Email'],
                    unique_identifier=unique_id,
                    mc_number=lead.get('MC Number', ''),
                    legal_name=lead.get('Legal Name', '')
                )
            except Exception as e:
                print(f"Failed to create EmailOpen log: {e}")
            
            tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
            personalized_body += tracking_pixel

        # 7. --- Send Email ---
        msg = EmailMultiAlternatives(
            subject=personalized_subject,
            body=personalized_body, # Fallback body (plain text)
            from_email=email_account.email_address,
            to=[lead['Email']],
            connection=connection
        )
        msg.extra_headers = {'Message-ID': message_id}
        msg.attach_alternative(personalized_body, "text/html")
        
        try:
            msg.send()
        except Exception as e:
            # Handle connection-lost error
            if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower():
                print("SMTP connection lost, reconnecting...")
                connection.close() # Close old
                connection = get_email_connection(email_account, decrypted_password)
                msg.connection = connection
                msg.send() # Retry send
            else:
                raise e # Re-raise other errors to be caught by outer try/except
        
        # 8. --- SUCCESS: Update DB & Log ---
        print(f"Celery Task: Sent to {lead['Email']} via {campaign.sender_account.email_address}")
        
        # # Pop the lead *after* successful send
        # campaign.leads_data.pop(0) 
        
        # # Update sent_count atomically and save the popped list
        # CampaignRecord.objects.filter(id=campaign.id).update(sent_count=F('sent_count') + 1)
        # campaign.save(update_fields=['leads_data'])

        with transaction.atomic():
            campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign.id)
            
            if lead == campaign_for_update.leads_data[0]:
                
                # Pop the lead from the instance's data
                campaign_for_update.leads_data.pop(0)
                # Increment the count on the instance
                campaign_for_update.sent_count += 1
                # save
                campaign_for_update.save(update_fields=['leads_data', 'sent_count'])

            # Update the in-memory 'campaign' object to reflect the changes
            campaign.leads_data = campaign_for_update.leads_data
            campaign.sent_count = campaign_for_update.sent_count


        # Create thread/message log
        if mailbox_instance:
            thread, _ = EmailThread.objects.get_or_create(
                mailbox=mailbox_instance,
                email1=email_account.email_address,
                email2=lead['Email'],
                subject=personalized_subject,
                defaults={'is_read': True}
            )
            OutgoingEmailMessage.objects.create(
                thread=thread,
                subject=personalized_subject,
                body=personalized_body,
                recipient=lead['Email'],
                sender=email_account.email_address,
                message_id=message_id,
                in_reply_to=None,
            )

    # 8. --- ERROR HANDLING ---
    except (CampaignRecord.DoesNotExist, EmailAccount.DoesNotExist) as e:
        print(f"Critical error: {e}. Stopping chain for campaign {campaign_record_id}.")
        # Don't reschedule
        return

    except Exception as e:
        print(f"Failed to send to {lead['Email']} (Campaign {campaign.id}): {e}")
        error_message = str(e)
        
        # A) Daily Limit Exceeded (Fatal, stop chain)
        if "Daily user sending limit exceeded" in error_message:
            print(f"Daily limit exceeded for {email_account.email_address}. Halting campaign {campaign.id}.")
            campaign.status = 'failed'
            campaign.save(update_fields=['status'])
            # ... (your notification_email logic) ...
            return # Stop the chain

        # B) Network/Timeout Error (Recoverable, retry task)
        elif "timeout" in error_message.lower() or "connection" in error_message.lower():
            print(f"Network error for campaign {campaign.id}. Retrying task.")
            # Retry the whole task (will re-peek the same lead)
            raise self.retry(exc=e, max_retries=3) 
        
        # C) Other Unhandled Error (Skip lead, continue chain)
        else:
            print(f"Unhandled error for {lead['Email']}: {e}. Skipping lead.")
            # Pop the lead to skip it, save, and continue to reschedule
            campaign.leads_data.pop(0)
            campaign.save(update_fields=['leads_data'])

    # 9. --- CLEANUP ---
    finally:
        if connection:
            connection.close()
            
    # 10. --- RESCHEDULE (if not stopped by an error) ---
    try:
        # Refresh campaign from DB to get latest state
        campaign.refresh_from_db() 
        
        # Check if complete
        if not campaign.leads_data:
            print(f"Campaign {campaign.id} finished.")
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
            return # Chain ends

        # Reschedule if still processing
        if campaign.status == 'processing':
            next_delay = random.randint(campaign.min_delay, campaign.max_delay)
            print(f"Rescheduling next email for {campaign.id} in {next_delay} seconds.")
            
            send_single_email.apply_async(
                args=[campaign_record_id],
                countdown=next_delay
            )
    except CampaignRecord.DoesNotExist:
        print(f"Campaign {campaign_record_id} was deleted. Stopping chain.")
    except Exception as e:
        print(f"Failed to reschedule campaign {campaign_record_id}: {e}")
        CampaignRecord.objects.filter(id=campaign_record_id).update(status='failed')


@shared_task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(email_account_id, leads, subject, body, min_delay, max_delay, campaign_record_id):
    """
    This is the "Kicker" task.
    It runs ONCE at the start of a campaign.
    Its only job is to populate the CampaignRecord and schedule the
    first processing task to run immediately.
    """
    print(f"Launching campaign {campaign_record_id} with {len(leads)} leads.")
    
    try:
        # Use transaction.atomic to ensure all DB writes succeed or fail together
        with transaction.atomic():
            campaign = CampaignRecord.objects.get(id=campaign_record_id)
            
            # Populate the campaign object with all necessary data
            campaign.leads_data = leads
            campaign.total_recipients = len(leads)
            campaign.sent_count = 0
            campaign.status = 'processing'
            
            # Store the templates and settings on the model
            campaign.subject = subject
            campaign.body = body
            campaign.min_delay = min_delay
            campaign.max_delay = max_delay
            campaign.sender_account_id = email_account_id
            
            campaign.save(update_fields=[
                'leads_data', 'total_recipients', 'sent_count', 'status',
                'subject', 'body', 'min_delay', 'max_delay', 'sender_account_id'
            ])

        # Schedule the *first* worker task.
        # It runs with countdown=0 (immediately).
        # The chain starts here.
        send_single_email.apply_async(
            args=[campaign_record_id],
            countdown=0
        )
        print(f"Campaign {campaign_record_id} successfully launched. First task queued.")

    except CampaignRecord.DoesNotExist:
        print(f"Failed to launch: CampaignRecord {campaign_record_id} does not exist.")
    except Exception as e:
        print(f"Critical error launching campaign {campaign_record_id}: {e}")
        # Optionally mark campaign as failed if setup fails
        try:
            CampaignRecord.objects.filter(id=campaign_record_id).update(status='failed')
        except:
            pass



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


@app.task(name="dashboard.tasks.clear_launched_campaigns")
def clear_launched_campaigns():
    """
    Deletes CampaignRecord entries where:
    - status is 'launched' AND
    - launch_time is older than 7 days
    """
    cutoff_date = timezone.now() - timedelta(days=7)
    status = ['launched', 'failed', 'cancelled']
    deleted_count, _ = CampaignRecord.objects.filter(
        status__in=status,
        launch_time__lt=cutoff_date
    ).delete()

