from growth_skool.celery import app
from celery import shared_task
from email.utils import make_msgid
from celery.exceptions import TimeLimitExceeded

from .models import DripCampaign, EmailAccountAndLeads, DripTemplate
from dashboard.models import GmailToken
from unibox.models import EmailThread, OutgoingEmailMessage

from dashboard.utilities import get_email_connection, personalize_template, sanitize_email_html, should_use_batch_processing
from .utilities import reschedule_or_finalize
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache

import re
import random
import uuid

email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
EMAIL_TASK_TIME_LIMIT = 600


# ===================================================================
# TASK 1: THE ALARM CLOCK
# ===================================================================
@app.task(name="drip_campaigns.check_scheduled_steps")
def check_scheduled_drip_step():
    
    now = timezone.now()
    campaigns_to_run = DripCampaign.objects.filter(
        status='Active',
        next_action_at__lte=now
    )

    if not campaigns_to_run.exists():
        return "No drip campaign steps due to run."

    for campaign in campaigns_to_run:
        try:
            with transaction.atomic():
                campaign.status = 'Processing'
                campaign.last_action_at = now
                campaign.next_action_at = None
                
                campaign.save(
                    update_fields=['status', 'last_action_at', 'next_action_at']
                )
            print("Calling the dispatcher")
            chain_starter_task.delay(campaign.id)
            
        except Exception as e:
            print(f"Error triggering campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])

    print(f"Triggered {len(campaigns_to_run)} campaign steps.")
    return


# ===================================================================
# TASK 2: THE DISPATCHER (Updated with Cache Logic)
# ===================================================================
@app.task(name="drip_campaigns.chain_starter_task")
def chain_starter_task(campaign_id):
    
    try:
        campaign = DripCampaign.objects.get(id=campaign_id)
        
        template = DripTemplate.objects.get(
            campaign=campaign, 
            step_number=campaign.current_step
        )
        
        all_accounts = campaign.email_accounts_and_leads.all()
        removed_leads_list = campaign.removed_mc_numbers


        total_accounts_for_step = 0
        
        # 1. Loop 1: Set the 'goal' counts for each account
        for account_info in all_accounts:
            valid_leads = [
                lead for lead in account_info.leads_data
                if lead.get('MC Number') not in removed_leads_list
            ]
            
            account_info.recipient_count = len(valid_leads)
            account_info.sent_count = 0
            account_info.save(update_fields=['recipient_count', 'sent_count'])

            if len(valid_leads) > 0:
                total_accounts_for_step += 1
        
        # 2. Update the template status
        template.delivered_status = 'Processing'
        template.save(update_fields=['delivered_status'])

        # 3. Set the counters in the cache
        cache_timeout = 86400 
        # Define unique keys for this specific step
        total_key = f"drip_step_total_{template.id}"
        finished_key = f"drip_step_finished_{template.id}"
        
        cache.set(total_key, total_accounts_for_step, timeout=cache_timeout)
        cache.set(finished_key, 0, timeout=cache_timeout)
        # ----------------------------
        
        if total_accounts_for_step == 0:
            print(f"Campaign {campaign.id} step {campaign.current_step} has no leads to send. Finalizing.")
            # No accounts to run, call finalizer immediately
            finalize_drip_step_task.delay(campaign_id)
            return "No leads to send for this step."

        # 4. Loop 2: Launch the worker tasks
        for account_info in all_accounts:
            if account_info.recipient_count > 0:
                use_batch = should_use_batch_processing(
                    campaign.min_delay, 
                    campaign.max_delay, 
                    batch_size=10
                )

                if use_batch:
                    # You need to create this batch task and
                    # implement the same cache logic in it
                    batch_sending_executor.apply_async(args=[campaign_id, account_info.id, template.id, 10], countdown=0)
                else:
                    send_single_email.apply_async(args=[campaign_id, account_info.id, template.id, 0], countdown=0)
                    print("Sending out single emails")
        
        return f"Dispatched {total_accounts_for_step} senders for campaign {campaign.id}, step {campaign.current_step}."

    except Exception as e:
        # ... (your error handling) ...
        print(f"Failed to start chain for campaign {campaign_id}: {e}")
        if 'campaign' in locals() and campaign:
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])
        if 'template' in locals() and template:
            template.delivered_status = 'Failed'
            template.save(update_fields=['delivered_status'])
        raise e


# ===================================================================
# TASK 3: THE WORKER (Updated)
# ===================================================================
@shared_task(name="drip_campaigns.send_single_email", acks_late=True, bind=True, default_retry_delay=300, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_single_email(self, campaign_id, account_info_id, template_id, lead_index):

    connection = None
    lead = None
    account = None
    template = None
    campaign = None
    # This is the index for the *next* task
    next_lead_index = lead_index + 1 

    try:
        template = DripTemplate.objects.get(id=template_id)
        account = EmailAccountAndLeads.objects.get(id=account_info_id)
        campaign = DripCampaign.objects.get(id=campaign_id)
        removed_leads_list = campaign.removed_mc_numbers

        if template.delivered_status in ('Sent', 'Failed'):
            print(f"Template {template.id} is already finished. Stopping chain.")
            return

        # --- Lead Fetching & Filtering ---
        # Use recipient_count (the goal) not leads_data (the full list)
        if lead_index >= account.recipient_count:
            print(f"Lead index {lead_index} is out of bounds for recipient goal {account.recipient_count}. Stopping.")
            # This task is done, trigger the finalize check
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

        lead = account.leads_data[lead_index]

        if lead.get('MC Number') in removed_leads_list:
            print(f"Skipping lead {lead.get('MC Number')} (in removed list).")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

        if not isinstance(lead, dict) or 'Email' not in lead or not re.fullmatch(email_regex, lead['Email']):
            print(f"Skipping invalid lead: {lead}.")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

        # --- Connection & Setup ---
        email_account = account.email_account
        decrypted_password = email_account.get_password()
        connection = get_email_connection(email_account, decrypted_password)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()

        # --- Prepare Email ---
        # *** FIX: Use template subject/body, not campaign ***
        personalized_subject = personalize_template(template.subject, lead)
        personalized_body = personalize_template(template.body, lead)
        
        message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
        DOMAIN = "https://dispatchskool.com"
        personalized_body = sanitize_email_html(personalized_body, DOMAIN)

        # --- Tracking ---
        # (Assuming you add a 'drip_campaign' ForeignKey to EmailOpen)
        # if template.track_template:
        #     unique_id = uuid.uuid4()
        #     pixel_url = reverse('dashboard:track_open', kwargs={'unique_identifier': unique_id})
        #     pixel_link = urljoin(settings.BASE_URL, pixel_url)
            
        #     try:
        #         # You'll need to add a ForeignKey 'drip_campaign' to your EmailOpen model
        #         EmailOpen.objects.create(
        #             drip_campaign=campaign, 
        #             recipient_email=lead['Email'],
        #             unique_identifier=unique_id,
        #             mc_number=lead.get('MC Number', ''),
        #             legal_name=lead.get('Legal Name', '')
        #         )
        #     except Exception as e:
        #         # Log error, but don't fail the send
        #         print(f"Failed to create EmailOpen log: {e}") 
            
        #     tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
        #     personalized_body += tracking_pixel

        # --- Send Email ---
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
                connection.close()
                connection = get_email_connection(email_account, decrypted_password)
                msg.connection = connection
                msg.send()
            else:
                raise e 
            
        print(f"Drip Task: Sent to {lead['Email']} via {account.email_account.email_address}")

        # --- Update Stats ---
        # We just increment the count. We don't need to pop.
        with transaction.atomic():
            account_to_update = EmailAccountAndLeads.objects.select_for_update().get(id=account_info_id)
            account_to_update.sent_count += 1
            account_to_update.save(update_fields=['sent_count'])

        # --- Create thread/message log ---
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

    except TimeLimitExceeded as e:
        print(f"Time limit exceeded for account {account_info_id}: {e}. Skipping lead {lead_index}.")
        reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=300)
        return

    except (DripTemplate.DoesNotExist, EmailAccountAndLeads.DoesNotExist, DripCampaign.DoesNotExist) as e:
        print(f"Critical error: {e}. Stopping chain for account {account_info_id}.")
        return

    except Exception as e:
        print(f"Failed to send to {lead['Email']} (Account {account_info_id}): {e}")
        error_message = str(e)
        
        if "Daily user sending limit exceeded" in error_message:
            print(f"Daily limit exceeded for {email_account.email_address}. Halting chain for this account.")
            # This account is done. Trigger the finalize logic.
            # We pass the *total* count to force it to finalize.
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        elif "timeout" in error_message.lower() or "connection" in error_message.lower():
            print(f"Network error for account {account_info_id}. Retrying task.")
            raise self.retry(exc=e, max_retries=3) 
        
        else:
            print(f"Unhandled error for {lead['Email']}: {e}. Skipping lead.")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

    # --- CLEANUP ---
    finally:
        if connection:
            connection.close()

    # --- RESCHEDULE ---
    next_delay = random.randint(campaign.min_delay, campaign.max_delay)
    reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=next_delay)


# ===================================================================
# TASK 4: THE FINISHER
# ===================================================================
@app.task(name="drip_campaigns.finalize_drip_step_task")
def finalize_drip_step_task(campaign_id):
    
    try:
        campaign = DripCampaign.objects.get(id=campaign_id)
        template = DripTemplate.objects.get(
            campaign=campaign, 
            step_number=campaign.current_step
        )
        
        if template.delivered_status == 'Sent':
            print(f"Finalizer: Step {template.step_number} already marked 'Sent'. Exiting.")
            return

        template.delivered_status = 'Sent'
        template.save(update_fields=['delivered_status'])
        
        # Find the *next available step* with a number
        # greater than the one we just finished.
        
        next_step_template = DripTemplate.objects.filter(
            campaign=campaign, 
            step_number__gt=campaign.current_step
        ).order_by('step_number').first() # Get the very next one
        
        if next_step_template:
            
            # Calculate its run time
            new_next_action_at = timezone.now() + campaign.step_delay
            
            # Update the campaign to point to the new step
            campaign.current_step = next_step_template.step_number
            campaign.status = 'Active' # "Unlock" it
            campaign.next_action_at = new_next_action_at # Set the new time
            
            campaign.save(
                update_fields=['current_step', 'status', 'next_action_at']
            )
            
            print(f"Step {template.step_number} finished. Queued Step {next_step_template.step_number}.")
            return
            
        else:
            campaign.status = 'Completed'
            campaign.next_action_at = None
            campaign.save(update_fields=['status', 'next_action_at'])
            
            print(f"Campaign {campaign.id} successfully completed.")
            return
            
    except Exception as e:
        print(f"Failed to finalize step for campaign {campaign_id}: {e}")
        DripCampaign.objects.filter(id=campaign_id).update(status='Failed')
        raise e


