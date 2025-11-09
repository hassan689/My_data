from users.models import EmailAccount, CustomUser
from unibox.models import EmailThread, OutgoingEmailMessage
from dashboard.models import GmailToken, CampaignRecord, EmailOpen

from django.utils import timezone
from django.conf import settings
from django.utils.timezone import timedelta
from django.urls import reverse
from django.utils.encoding import force_str
from django.db import transaction

import re
import random
import uuid
import time

from email.utils import make_msgid
from urllib.parse import urljoin
from growth_skool.celery import app
from celery import shared_task
from celery.exceptions import TimeLimitExceeded

from .utilities import get_email_connection, personalize_template, sanitize_email_html, should_use_batch_processing
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail, EmailMessage


email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


# Set a time limit *less* than RabbitMQ's 30-min timeout
# (e.g., 10 minutes = 600 seconds)
EMAIL_TASK_TIME_LIMIT = 600

@shared_task(name="dashboard.send_single_email", acks_late=True, bind=True, default_retry_delay=300, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_single_email(self, campaign_record_id):
    """
    This is the self-perpetuating "Worker" task.
    It processes ONE lead, saves progress, and reschedules itself.
    - acks_late=True: Prevents task loss if the worker crashes mid-send.
    - bind=True: Allows us to call self.retry() for network errors.
    """
    connection = None
    lead = None
    email_account = None
    campaign = None
    lead_processed_by_this_worker = False

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

        with transaction.atomic():
            campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign.id)
            
            if lead == campaign_for_update.leads_data[0]:
                
                # Pop the lead from the instance's data
                campaign_for_update.leads_data.pop(0)
                # Increment the count on the instance
                campaign_for_update.sent_count += 1
                # save
                campaign_for_update.save(update_fields=['leads_data', 'sent_count'])
                lead_processed_by_this_worker = True

            # Update the in-memory 'campaign' object to reflect the changes
            campaign.leads_data = campaign_for_update.leads_data
            campaign.sent_count = campaign_for_update.sent_count

        if not lead_processed_by_this_worker:
            return

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

    except TimeLimitExceeded as e:
        print(f"Time limit exceeded for campaign {campaign_record_id}: {e}. Retrying task.")
        try:
            # Atomically remove the lead that timed out
            with transaction.atomic():
                campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign.id)
                
                if not campaign_for_update.leads_data or campaign_for_update.leads_data[0]['Email'] != lead['Email']:
                    print(f"Timeout handler: Lead {lead['Email']} was already processed. Stopping.")
                    return # Another worker already handled it, or it's empty

                # Pop the bad lead
                campaign_for_update.leads_data.pop(0)
                campaign_for_update.save(update_fields=['leads_data'])
                
                # Update in-memory object
                campaign.leads_data = campaign_for_update.leads_data

            # Reschedule for the *next* lead after a 5-min safety delay
            if campaign.leads_data:
                print(f"Timeout handler: Scheduling next lead for {campaign.id} in 5 minutes.")
                send_single_email.apply_async(
                    args=[campaign_record_id],
                    countdown=300 # 5 minutes
                )
            else:
                print(f"Timeout handler: Skipped last lead. Campaign {campaign.id} finished.")
                campaign.status = 'launched'
                campaign.save(update_fields=['status'])

        except Exception as skip_e:
            # If skipping fails, we have a bigger problem. Fail the campaign.
            print(f"CRITICAL: Failed to skip timed-out lead! Failing campaign {campaign.id}. Error: {skip_e}")
            CampaignRecord.objects.filter(id=campaign_record_id).update(status='failed')
            
        return # Stop the current (timed-out) task

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
            campaign.status = 'launched'
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


@shared_task(name="dashboard.send_emails_batch", acks_late=True, bind=True, default_retry_delay=300, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_emails_batch(self, campaign_record_id, batch_size=10):
    """
    Batch processor that updates count and lead list atomically *per email*.
    """
    connection = None
    campaign = None
    email_account = None
    iter_count = 0

    try:
        campaign = CampaignRecord.objects.get(id=campaign_record_id)
        
        if campaign.status in ('cancelled', 'launched', 'failed'):
            print(f"Campaign {campaign.id} is finished or cancelled. Stopping batch processing.")
            return
            
        if not campaign.leads_data:
            print(f"Campaign {campaign.id} has no leads left. Finishing.")
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
            return
            
        # Get current batch size. This list is static for the loop.
        current_batch = campaign.leads_data[:batch_size]
        
        # Setup SMTP connection for the batch
        email_account = campaign.sender_account
        decrypted_password = email_account.get_password()
        connection = get_email_connection(email_account, decrypted_password)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()
        
        # Process each lead in the static batch
        for lead in current_batch:
            iter_count += 1 # Increment just to count the iteration
            try:
                # Basic lead validation
                if not isinstance(lead, dict) or 'Email' not in lead:
                    print(f"Skipping invalid lead: {lead}")
                    continue
                    
                if not re.fullmatch(email_regex, lead['Email']):
                    print(f"Skipping invalid email: {lead['Email']}")
                    continue
                
                # Prepare email content
                personalized_subject = personalize_template(campaign.subject, lead)
                personalized_body = personalize_template(campaign.body, lead)
                message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
                DOMAIN = "https://dispatchskool.com"
                personalized_body = sanitize_email_html(personalized_body, DOMAIN)
                
                # Add tracking pixel if enabled
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
                        tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
                        personalized_body += tracking_pixel
                    except Exception as e:
                        print(f"Failed to create EmailOpen log: {e}")
                
                # Send email
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
                    # Handle connection-lost error
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower():
                        print("SMTP connection lost, reconnecting...")
                        connection.close() # Close old
                        connection = get_email_connection(email_account, decrypted_password)
                        msg.connection = connection
                        msg.send() # Retry send
                    else:
                        raise e # Re-raise other errors to be caught by outer try/except
                
                # --- SUCCESS: ATOMIC UPDATE ---
                # Send was successful. Increment count AND remove lead.
                with transaction.atomic():
                    campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
                    campaign_for_update.sent_count += 1
                    
                    current_leads = campaign_for_update.leads_data or []
                    try:
                        # Find and remove this specific lead
                        current_leads.remove(lead)
                        campaign_for_update.leads_data = current_leads
                    except ValueError:
                        # This should not happen if logic is correct, but as a safeguard
                        print(f"Warning: Sent lead {lead.get('Email')} but it was not in leads_data.")
                    
                    campaign_for_update.save(update_fields=['sent_count', 'leads_data'])
                    # Update local campaign object for the next loop's "if" check
                    campaign.leads_data = campaign_for_update.leads_data

                print(f"Celery Task: Sent to {lead['Email']} via {campaign.sender_account.email_address}")

                # --- (Thread/message log creation) ---
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
                
                if iter_count < len(current_batch):
                    # Apply delay between emails
                    time.sleep(random.randint(campaign.min_delay, campaign.max_delay))

            except Exception as e:
                # --- FAILURE: ATOMIC UPDATE ---
                # Send failed. *Only* remove lead, DO NOT increment count.
                # This prevents infinite retries on a bad lead.
                print(f"Error processing lead {lead['Email']}: {e}. Skipping and removing.")
                with transaction.atomic():
                    campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
                    current_leads = campaign_for_update.leads_data or []
                    try:
                        current_leads.remove(lead)
                        campaign_for_update.leads_data = current_leads
                    except ValueError:
                        print(f"Warning: Failed lead {lead.get('Email')} was already removed.")
                    
                    campaign_for_update.save(update_fields=['leads_data'])
                    # Update local campaign object
                    campaign.leads_data = campaign_for_update.leads_data
                
                # Continue to the next lead in the batch
                continue
                
        # --- End of batch processing ---
        
        # We need to re-fetch the campaign state as it was modified in the loop
        campaign = CampaignRecord.objects.get(id=campaign_record_id)
        
        if campaign.leads_data:
            print(f"Scheduling next batch for campaign {campaign.id}")
            send_emails_batch.apply_async(
                args=[campaign_record_id],
                countdown=campaign.max_delay  # delay between batches
            )
        else:
            print(f"Campaign {campaign.id} finished")
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
            
    except Exception as e:
        print(f"Batch processing error: {e}")
        if campaign:
            campaign.status = 'failed'
            campaign.save(update_fields=['status'])
    finally:
        if connection:
            connection.close()


@shared_task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(campaign_record_id):
    """
    This is the "Kicker" task.
    It runs ONCE at the start of a campaign.
    Its only job is to populate the CampaignRecord and schedule the
    first processing task to run immediately.

    Modified kicker task that chooses between single or batch processing
    based on the campaign's delay settings.
    """
    try:
        campaign = CampaignRecord.objects.get(id=campaign_record_id)
        leads = campaign.leads_data or []
        print(f"Launching campaign {campaign_record_id} with {len(leads)} leads.")

        # Ensure campaign object is in the expected initial state
        with transaction.atomic():
            campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
            campaign.total_recipients = len(leads)
            campaign.sent_count = campaign.sent_count or 0
            if campaign.status in ('pending', None):
                campaign.status = 'processing'

            # Save fields which might have been changed by the UI already
            campaign.save(update_fields=['total_recipients', 'sent_count', 'status'])

        # # Schedule the *first* worker task immediately.
        # send_single_email.apply_async(args=[campaign_record_id], countdown=0)
        # print(f"Campaign {campaign_record_id} successfully launched. First task queued.")

        # Determine processing mode based on delay settings
        if should_use_batch_processing(campaign.min_delay, campaign.max_delay, batch_size=10):
            print(f"Using batch processing for campaign {campaign_record_id}.")
            send_emails_batch.apply_async(args=[campaign_record_id, 10], countdown=0)
        else:
            print(f"Using single email processing for campaign {campaign_record_id}.")
            send_single_email.apply_async(args=[campaign_record_id], countdown=0)
            
        print(f"Campaign {campaign_record_id} successfully launched.")

    except CampaignRecord.DoesNotExist:
        print(f"Failed to launch: CampaignRecord {campaign_record_id} does not exist.")
    except Exception as e:
        print(f"Critical error launching campaign {campaign_record_id}: {e}")



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

            # Trigger the kicker task by campaign id only; task will fetch leads and settings from DB
            send_emails_chunk_celery_task.delay(campaign_record.id)
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



@app.task(name="dashboard.tasks.revive_failed_launch")
def revive_failed_launch():
    
    CampaignRecord.objects.filter(status='processing', sent_count=0).update(
        status='pending',
        scheduled_launch_time=timezone.now()
    )
    launch_scheduled_campaign_checker() # Immediately launch the above campaigns that were set to pending



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
    - status is 'launched', 'failed' or 'cancelled' AND
    - launch_time is older than 7 days
    """
    cutoff_date = timezone.now() - timedelta(days=7)
    status = ['launched', 'failed', 'cancelled']
    deleted_count, _ = CampaignRecord.objects.filter(
        status__in=status,
        launch_time__lt=cutoff_date
    ).delete()

