from users.models import EmailAccount, CustomUser
from unibox.models import EmailThread, OutgoingEmailMessage
from dashboard.models import GmailToken, CampaignRecord, EmailOpen

from django.utils import timezone
from django.conf import settings
from django.utils.timezone import timedelta
from django.urls import reverse
from django.utils.encoding import force_str
from django.db import transaction
from django_celery_results.models import TaskResult

import re
import random
import uuid
import time

from email.utils import make_msgid
from urllib.parse import urljoin
from growth_skool.celery import app
from celery import shared_task, states
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
        with transaction.atomic():
            # 1. --- Get Campaign ---
            campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)

            if campaign.status in ('cancelled', 'launched', 'failed'):
                print(f"Campaign {campaign.id} is finished or cancelled. Stopping chain.")
                return

            if not campaign.leads_data:
                print(f"Campaign {campaign.id} has no leads left. Finishing.")
                campaign.status = 'launched'
                campaign.save(update_fields=['status'])
                return

            lead = campaign.leads_data[0] # Get the first lead

            # ✅ NEW: Ensure sent_emails list exists
            if campaign.sent_emails is None:
                campaign.sent_emails = []

            # ✅ NEW: Check if already sent
            if lead.get('Email') in campaign.sent_emails:
                print(f"Skipping duplicate email {lead['Email']} for campaign {campaign.id}.")
                # Pop and reschedule for next lead
                campaign.leads_data.pop(0)
                campaign.save(update_fields=['leads_data'])

                # Check if campaign completed
                if not campaign.leads_data: # No lead left
                    print(f"Campaign {campaign.id} finished.")
                    campaign.status = 'launched'
                    campaign.save(update_fields=['status'])
                    return # Chain ends

                # Reschedule if still processing
                if campaign.status == 'processing':
                    print(f"Rescheduling next email for {campaign.id}.")
                    
                    send_single_email.apply_async(
                        args=[campaign_record_id],
                        countdown=5 # the campaign has already waited before coming to this lead so no need to wait again
                    )
                    return
        
            
            # --- LEAD IS GOOD ---
            # We are the first. We "claim" this lead by popping it
            # and adding it to the sent list.
            campaign.leads_data.pop(0)
            campaign.sent_emails.append(lead['Email'])
            # We do NOT increment sent_count here. We do it *after* the send.
            
            campaign.save(update_fields=['leads_data', 'sent_emails'])
        
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
            campaign_for_update.sent_count += 1
            campaign_for_update.save(update_fields=['sent_count'])
            lead_processed_by_this_worker = True

            # Update the in-memory 'campaign' object to reflect the changes
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

            subject = f"⚠️ Campaign Halted: Daily Sending Limit Exceeded for {email_account.email_address}"
            body = (
                f"Dear user,\n\n"
                f"Your email campaign using the account '{email_account.email_address}' has been halted "
                f"because **Your Email Provider has indicated that the daily sending limit for this email account has been exceeded.**\n\n"
                f"**This limit is imposed by Your Email Provider, not by DispatchSkool.**\n\n"
                f"Please wait 24 hours before trying to send new campaigns from this account.\n\n"
                f"Regards,\nThe DispatchSkool Team"
            )
            from_email = settings.EMAIL_HOST_USER
            recipient_list = [email_account.user.email]
            body_encoded = force_str(body, 'utf-8', errors='replace')

            send_mail(
                subject=subject,
                message=body_encoded,
                from_email=from_email,
                recipient_list=recipient_list,
                fail_silently=False,
            )

            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
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
    Batch processor that atomically "pops" a batch of leads
    and then processes them.
    """
    connection = None
    email_account = None
    iter_count = 0
    current_batch = [] # Will be populated by the atomic pop
    campaign_for_loop = None # Holds campaign obj for use outside the transaction

    try:
        # --- 1. ATOMIC POP ---
        # Atomically "claim" a batch of leads so no other worker
        # can process them at the same time.
        with transaction.atomic():
            campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)

            if campaign.sent_emails is None:
                campaign.sent_emails = []
            
            if campaign.status in ('cancelled', 'launched', 'failed'):
                print(f"Campaign {campaign.id} is finished or cancelled. Stopping.")
                return
                
            if not campaign.leads_data:
                print(f"Campaign {campaign.id} has no leads left. Finishing.")
                if campaign.status != 'launched': # Avoid redundant DB write
                    campaign.status = 'launched'
                    campaign.save(update_fields=['status'])
                return
            
            # This is the "atomic pop"
            current_batch = campaign.leads_data[:batch_size]
            remaining_leads = campaign.leads_data[batch_size:]

            current_batch = [lead for lead in current_batch 
                if isinstance(lead, dict) and lead.get('Email') not in (campaign.sent_emails or [])]
            
            campaign.leads_data = remaining_leads
            campaign.save(update_fields=['leads_data'])

            if not current_batch:
                print(f"All leads already sent for campaign {campaign.id}. Finishing.")
                campaign.status = 'launched'
                campaign.save(update_fields=['status'])
                return
            
            # We need this for the loop
            campaign_for_loop = campaign 
        
        # --- 2. PROCESS THE BATCH (OUTSIDE THE LOCK) ---
        # 'current_batch' is now exclusively owned by this task.
        
        email_account = campaign_for_loop.sender_account
        decrypted_password = email_account.get_password()
        connection = get_email_connection(email_account, decrypted_password)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()
        
        for lead in current_batch:
            
            # Dbl check
            if lead['Email'] in (campaign_for_loop.sent_emails or []):
                print(f"Skipping duplicate lead inside batch: {lead['Email']}")
                continue

            iter_count += 1
            try:
                # --- 3. VALIDATE AND PREPARE LEAD ---
                if not isinstance(lead, dict) or 'Email' not in lead:
                    print(f"Skipping invalid lead: {lead}")
                    continue
                        
                if not re.fullmatch(email_regex, lead['Email']):
                    print(f"Skipping invalid email: {lead['Email']}")
                    continue
                
                # Prepare email content
                personalized_subject = personalize_template(campaign_for_loop.subject, lead)
                personalized_body = personalize_template(campaign_for_loop.body, lead)
                message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
                DOMAIN = "https://dispatchskool.com"
                personalized_body = sanitize_email_html(personalized_body, DOMAIN)
                
                # Add tracking pixel if enabled
                if campaign_for_loop.track_campaign:
                    unique_id = uuid.uuid4()
                    pixel_url = reverse('dashboard:track_open', kwargs={'unique_identifier': unique_id})
                    pixel_link = urljoin(settings.BASE_URL, pixel_url)
                    
                    try:
                        EmailOpen.objects.create(
                            campaign=campaign_for_loop,
                            recipient_email=lead['Email'],
                            unique_identifier=unique_id,
                            mc_number=lead.get('MC Number', ''),
                            legal_name=lead.get('Legal Name', '')
                        )
                        tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
                        personalized_body += tracking_pixel
                    except Exception as e:
                        print(f"Failed to create EmailOpen log: {e}")
                
                # --- 4. SEND EMAIL ---
                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body, # Will be replaced by HTML
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
                        connection.close()
                        connection = get_email_connection(email_account, decrypted_password)
                        msg.connection = connection
                        msg.send() # Retry send
                    else:
                        raise e # Re-raise to be caught by outer loop
                
                # --- 5. SUCCESS: ATOMIC COUNT ---
                # Send was successful. Now we *only* increment the count.
                # We DON'T touch leads_data here.
                with transaction.atomic():
                    campaign_for_update = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
                    campaign_for_update.sent_count += 1

                    # Initialize if missing
                    if campaign_for_update.sent_emails is None:
                        campaign_for_update.sent_emails = []

                    # Append the sent email
                    campaign_for_update.sent_emails.append(lead['Email'])
                    campaign_for_update.save(update_fields=['sent_count', 'sent_emails'])

                print(f"Celery Task: Sent to {lead['Email']} via {campaign_for_loop.sender_account.email_address}")

                # --- 6. CREATE LOGS (Thread/message) ---
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
                    # Apply delay *between* emails in the batch
                    time.sleep(random.randint(campaign_for_loop.min_delay, campaign_for_loop.max_delay))

            except Exception as e:
                # --- 7. FAILURE: SKIP LEAD ---
                # We just log and continue to the next lead.
                # We DO NOT increment sent_count.
                print(f"Error processing lead {lead['Email']} (Campaign {campaign_record_id}): {e}. Skipping.")
                continue
                
        # --- 8. RESCHEDULE (if needed) ---
        # Re-fetch the *latest* campaign state to check if more leads remain
        campaign = CampaignRecord.objects.get(id=campaign_record_id)
        
        if campaign.leads_data:
            print(f"Scheduling next batch for campaign {campaign.id}")
            send_emails_batch.apply_async(
                args=[campaign_record_id],
                kwargs={'batch_size': batch_size}, # Pass batch_size along
                countdown=campaign_for_loop.max_delay # delay *between* batches
            )
        else:
            print(f"Campaign {campaign.id} finished")
            campaign.status = 'launched'
            campaign.save(update_fields=['status'])

    except TimeLimitExceeded:
        print(f"CRITICAL: Time limit exceeded for batch task {campaign_record_id}.")
        
        # The lead being processed (at iter_count) is lost.
        # We re-queue the *rest* of the batch.
        unprocessed_leads = current_batch[iter_count:] 
        
        if unprocessed_leads:
            print(f"Re-queueing {len(unprocessed_leads)} unprocessed leads.")
            try:
                with transaction.atomic():
                    campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
                    current_leads = campaign.leads_data or []
                    campaign.leads_data = unprocessed_leads + current_leads
                    campaign.save(update_fields=['leads_data'])
            except Exception as e:
                print(f"CRITICAL: Failed to re-queue leads. Failing campaign. Error: {e}")
                CampaignRecord.objects.filter(id=campaign_record_id).update(status='failed')
                return # Stop here if re-queueing failed

        # We must also re-schedule the next task chain
        print(f"Rescheduling next batch for {campaign_record_id} after timeout.")
        send_emails_batch.apply_async(
            args=[campaign_record_id],
            kwargs={'batch_size': batch_size},
            countdown=60 # 1 min safety delay
        )

    except Exception as e:
        # This is a major error (e.g., DB down, or the atomic pop failed)
        print(f"CRITICAL: Batch processing error for {campaign_record_id}: {e}")

        if "Daily user sending limit exceeded" in str(e):
            print(f"Daily limit exceeded for {email_account.email_address}. Halting campaign {campaign.id}.")

            subject = f"⚠️ Campaign Halted: Daily Sending Limit Exceeded for {email_account.email_address}"
            body = (
                f"Dear user,\n\n"
                f"Your email campaign using the account '{email_account.email_address}' has been halted "
                f"because **Your Email Provider has indicated that the daily sending limit for this email account has been exceeded.**\n\n"
                f"**This limit is imposed by Your Email Provider, not by DispatchSkool.**\n\n"
                f"Please wait 24 hours before trying to send new campaigns from this account.\n\n"
                f"Regards,\nThe DispatchSkool Team"
            )
            from_email = settings.EMAIL_HOST_USER
            recipient_list = [email_account.user.email]
            body_encoded = force_str(body, 'utf-8', errors='replace')

            send_mail(
                subject=subject,
                message=body_encoded,
                from_email=from_email,
                recipient_list=recipient_list,
                fail_silently=False,
            )

            campaign.status = 'launched'
            campaign.save(update_fields=['status'])
            return # Stop the chain

        
        # Re-queue all leads from the batch that were not *started*
        # If error was at get_connection, iter_count=0, all leads are saved.
        unprocessed_leads = current_batch[iter_count:]

        if unprocessed_leads:
            print(f"Re-queueing {len(unprocessed_leads)} unprocessed leads after critical error.")
            try:
                with transaction.atomic():
                    campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)
                    current_leads = campaign.leads_data or []
                    campaign.leads_data = unprocessed_leads + current_leads
                    campaign.save(update_fields=['leads_data'])
            except Exception as e:
                print(f"CRITICAL: Failed to re-queue leads. Failing campaign. Error: {e}")
                CampaignRecord.objects.filter(id=campaign_record_id).update(status='failed')
                return # Stop here

        # Reschedule with a delay
        print(f"Rescheduling next batch for {campaign_record_id} after critical error.")
        send_emails_batch.apply_async(
            args=[campaign_record_id],
            kwargs={'batch_size': batch_size},
            countdown=60 # 1 min safety delay
        )
        
    finally:
        if connection:
            connection.close()


@shared_task(name="dashboard.send_emails_chunk_celery_task")
def send_emails_chunk_celery_task(campaign_record_id):
    """
    Idempotent kicker task.
    Uses a dedicated 'is_campaign_dispatched' flag to ensure it
    only schedules the first worker task ONCE, even if this
    kicker task is run multiple times.
    """
    try:
        # We need to know if we are the one to schedule the task
        should_dispatch_worker = False

        with transaction.atomic():
            campaign = CampaignRecord.objects.select_for_update().get(id=campaign_record_id)

            if not campaign.is_campaign_dispatched:
                # We are the first! Mark it as scheduled.
                campaign.is_campaign_dispatched = True
                campaign.save(update_fields=['is_campaign_dispatched'])
                
                # Tell the code outside the transaction to schedule the task
                should_dispatch_worker = True
        
            if should_dispatch_worker:
                campaign = CampaignRecord.objects.get(id=campaign_record_id)
                leads = campaign.leads_data or []
                
                campaign.total_recipients = len(leads)
                campaign.sent_count = 0 # Even if it is cancelled and resumed, it should start fresh with the remaining leads and sent_count
                campaign.save(update_fields=['total_recipients', 'sent_count'])

                if should_use_batch_processing(campaign.min_delay, campaign.max_delay, batch_size=10):
                    print(f"Using batch processing for campaign {campaign_record_id}.")
                    send_emails_batch.apply_async(args=[campaign_record_id, 10], countdown=0)
                else:
                    print(f"Using single email processing for campaign {campaign_record_id}.")
                    send_single_email.apply_async(args=[campaign_record_id], countdown=0)
                    
                print(f"Campaign {campaign_record_id} successfully launched.")
                
            else:
                # A duplicate task ran, but we safely ignored it.
                print(f"Campaign {campaign_record_id} worker was already scheduled. Ignoring duplicate kicker task.")

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
        scheduled_launch_time=timezone.now(),
        is_campaign_dispatched=False
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


@app.task(name="dashboard.cleanup_old_task_results")
def cleanup_old_task_results():
    """
    Deletes Celery task results from the django_celery_results backend
    that are older than 2 hours.
    """
    try:
        # Calculate the cutoff time
        cutoff_time = timezone.now() - timedelta(hours=2)
        
        old_tasks = TaskResult.objects.filter(
            date_done__lt=cutoff_time,
            status__in=[states.SUCCESS, states.FAILURE]
        )
        
        # Delete the old tasks and get the count
        deleted_count, _ = old_tasks.delete()
        
        if deleted_count > 0:
            print(f"Successfully deleted {deleted_count} task results older than 3 hours.")
        else:
            print("No old task results to delete.")
            
        return f"Deleted {deleted_count} tasks."

    except Exception as e:
        print(f"Error during task result cleanup: {e}")
        raise

