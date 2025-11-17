from growth_skool.celery import app
from celery import shared_task, chord
from email.utils import make_msgid
from celery.exceptions import TimeLimitExceeded

from .models import DripCampaign, EmailAccountAndLeads, DripTemplate, SentDripEmail
from dashboard.models import GmailToken
from unibox.models import EmailThread, OutgoingEmailMessage

from dashboard.utilities import get_email_connection, personalize_template, sanitize_email_html, should_use_batch_processing
from .utilities import reschedule_or_finalize, normalize_provider
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache

import re
import random
import uuid
import imaplib
import email
import time


email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
EMAIL_TASK_TIME_LIMIT = 600

# The IMAP settings map. We will look up settings here.
# (Using SSL ports by default)
IMAP_SETTINGS_MAP = {
    'gmail':    {'host': 'imap.gmail.com', 'port': 993},
    'yahoo':    {'host': 'imap.mail.yahoo.com', 'port': 993},
    'zoho':     {'host': 'imap.zoho.com', 'port': 993},
    'hostinger':{'host': 'imap.hostinger.com', 'port': 993},
    'namecheap':{'host': 'imap.privateemail.com', 'port': 993},
}


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
            print("Checking for replies!")
            check_campaign_replies_task.delay(campaign.id)
            
        except Exception as e:
            print(f"Error triggering campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])

    print(f"Triggered {len(campaigns_to_run)} campaign steps.")
    return



# ===================================================================
# TASK 1.1: THE Coordinator

# This is a new task. Its job is to coordinate the parallel IMAP checks. 
# It uses a Celery chord to "fan out" the work to many worker tasks and "fan in" to a single callback.
# ===================================================================
@app.task(name="drip_campaigns.check_campaign_replies_task")
def check_campaign_replies_task(campaign_id):
    
    try:
        campaign = DripCampaign.objects.get(id=campaign_id)
        all_accounts_info = campaign.email_accounts_and_leads.all()

        if not all_accounts_info.exists():
            # No accounts, just go straight to sending
            chain_starter_task.delay(None, campaign.id)
            return "No accounts to check."

        # 1. Create a "group" of all the worker tasks to run in parallel
        imap_check_group = []
        for account_info in all_accounts_info:
            # .s() creates a "signature" for the task
            imap_check_group.append(
                check_one_account_imap.s(account_info.id)
            )

        # 2. Define the "callback" task.
        #    This is the task that will run *only when* all tasks
        #    in the group are 100% complete.
        callback = chain_starter_task.s(campaign.id)

        # 3. Launch the Chord
        #    This says: "Run all tasks in the group, and when they are
        #    all done, run the callback."
        chord(imap_check_group)(callback)

        return f"Launched reply-check chord for {len(imap_check_group)} accounts."

    except Exception as e:
        print(f"Failed to start reply-check chord for campaign {campaign.id}: {e}")
        # If the chord fails to launch, fail the campaign
        DripCampaign.objects.filter(id=campaign_id).update(status='Failed')
        raise e



# ===================================================================
# TASK 1.2: THE IMAP Worker

# This is the new task that does the actual IMAP work, incorporating your "history point" logic.
# ===================================================================
@shared_task(name="drip_campaigns.check_one_account_imap", bind=True, acks_late=True, default_retry_delay=300, time_limit=EMAIL_TASK_TIME_LIMIT)
def check_one_account_imap(self, account_info_id):
    imap_conn = None
    account_info = None
    try:
        # 1. Fetch models
        account_info = EmailAccountAndLeads.objects.get(id=account_info_id)
        email_account = account_info.email_account
        campaign = account_info.campaign

        # 2. --- Get IMAP Settings ---
        normalized_provider = normalize_provider(email_account.email_provider)
        imap_settings = IMAP_SETTINGS_MAP.get(normalized_provider)
        
        if not imap_settings:
            print(f"Skipping reply check for {email_account.email_address}: Provider '{email_account.email_provider}' is not recognized.")
            account_info.last_reply_check_at = timezone.now()
            account_info.save(update_fields=['last_reply_check_at'])
            return f"Skipped: Provider '{email_account.email_provider}' not recognized."

        # 3. Implement "History Point" Logic
        start_check_time = account_info.last_reply_check_at or campaign.created_at
        imap_search_date = start_check_time.strftime("%d-%b-%Y")

        # 4. Log in to IMAP (using the correct settings)
        imap_conn = imaplib.IMAP4_SSL(imap_settings['host'], imap_settings['port'])
        imap_conn.login(email_account.email_address, email_account.get_password())
        imap_conn.select("INBOX")

        # 5. ✅ Search for *all* messages (not just unread) since last check
        search_query = f'(SINCE "{imap_search_date}")'
        status, email_ids = imap_conn.search(None, search_query)

        if status != 'OK':
            raise Exception("IMAP search failed")

        found_replies = 0
        email_id_list = email_ids[0].split()

        # 6. Loop through found emails
        for e_id in email_id_list:
            # ✅ Fetch full message (not just headers)
            status, msg_data = imap_conn.fetch(e_id, '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])

            # ✅ More reliable header extraction
            in_reply_to_header = msg.get('In-Reply-To') or msg.get('References')
            if not in_reply_to_header:
                continue

            # 1. Clean and SPLIT the header into a list of potential IDs
            # This turns "<id1> <id2>" into ["<id1>", "<id2>"]
            potential_ids = [
                pid.strip().replace('<', '').replace('>', '')
                for pid in in_reply_to_header.replace('\n', '').replace('\r', '').split()
            ]

            if not potential_ids:
                continue # Skip if the header was just whitespace

            # 2. Check our "Paper Trail" using an '__in' lookup
            # This checks if ANY of our saved message_ids are in the list
            sent_email = SentDripEmail.objects.filter(
                drip_campaign=campaign,
                message_id__in=potential_ids,
            ).first()

            # 8. MATCH FOUND!
            if sent_email:
                print(f"✅ Reply found for {sent_email.lead_email} in campaign {campaign.id}!")
                sent_email.status = 'Replied'
                sent_email.save(update_fields=['status'])

                lead_identifier = sent_email.lead_email or sent_email.lead_mc_number

                # 9. Add to the campaign's "stop list"
                with transaction.atomic():
                    campaign_to_update = DripCampaign.objects.select_for_update().get(id=campaign.id)
                    if lead_identifier not in campaign_to_update.removed_mc_numbers:
                        campaign_to_update.removed_mc_numbers.append(lead_identifier)
                        campaign_to_update.save(update_fields=['removed_mc_numbers'])

                found_replies += 1
                # ✅ Mark as seen
                imap_conn.store(e_id, '+FLAGS', '\\Seen')

        # 10. Update our "History Point"
        account_info.last_reply_check_at = timezone.now()
        account_info.save(update_fields=['last_reply_check_at'])

        return f"Checked {email_account.email_address}. Found {found_replies} replies."

    except Exception as e:
        print(f"❌ Failed to check IMAP for {account_info_id}: {e}")
        if account_info:
            account_info.last_reply_check_at = timezone.now()
            account_info.save(update_fields=['last_reply_check_at'])
        raise self.retry(exc=e, max_retries=3)
    
    finally:
        if imap_conn:
            try:
                imap_conn.close()
                imap_conn.logout()
            except Exception:
                pass


# ===================================================================
# TASK 2: THE DISPATCHER
# ===================================================================
@app.task(name="drip_campaigns.chain_starter_task")
def chain_starter_task(results, campaign_id):
    
    try:
        campaign = DripCampaign.objects.get(id=campaign_id)
        
        template = DripTemplate.objects.get(
            campaign=campaign, 
            step_number=campaign.current_step
        )
        
        all_accounts = campaign.email_accounts_and_leads.all()
        removed_leads_set = set(campaign.removed_mc_numbers)

        sent_leads_set = set(SentDripEmail.objects.filter(
            drip_campaign=campaign, 
            template=template
        ).values_list('lead_email', flat=True))

        total_accounts_for_step = 0
        
        # 1. Loop 1: Set the 'goal' counts for each account
        for account_info in all_accounts:
            valid_leads = []
            for lead in account_info.leads_data:
                mc_num = lead.get('MC Number')
                email_addr = lead.get('Email')

                # Check if either identifier is in the "stop list" or "already sent to for this template" (used for resuming campaigns)
                if (mc_num and mc_num in removed_leads_set) or (email_addr and email_addr in removed_leads_set) or (email_addr and email_addr in sent_leads_set):
                    continue
                
                valid_leads.append(lead)
            
            account_info.recipient_count = len(valid_leads)
            account_info.sent_count = 0

            # Set the new status
            if len(valid_leads) > 0:
                account_info.status = 'Processing'
                total_accounts_for_step += 1
            else:
                account_info.status = 'Ready'
            
            account_info.save(update_fields=['recipient_count', 'sent_count', 'status'])
        
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
                    send_batch_emails.apply_async(args=[campaign_id, account_info.id, template.id, 0, 10], countdown=0)
                    print("Sending out Batch of emails")
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
# TASK(S) 3: THE WORKERS
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
        removed_leads_set = set(campaign.removed_mc_numbers)

        if campaign.status == 'Cancelled':
            print(f"Campaign {campaign.id} was cancelled. Stopping chain.")
            return

        # Check 2: Has this specific account chain been stopped?
        if account.status == 'Stopped':
            print(f"Account {account.id} was manually stopped. Stopping chain.")
            # telling the system that this account is done for its leads
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        # Check 3: Has the entire step been finished or skipped?
        if template.delivered_status in ('Sent', 'Failed', 'Cancelled'):
            print(f"Template {template.id} is finished or {template.delivered_status}. Stopping chain.")
            # We call reschedule_or_finalize to "clock out"
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        # --- Lead Fetching & Filtering ---
        # Use recipient_count (the goal) not leads_data (the full list)
        if lead_index >= len(account.leads_data):
            print(f"Lead index {lead_index} is out of bounds for recipient goal {account.recipient_count}. Stopping.")
            # This task is done, trigger the finalize check
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

        lead = account.leads_data[lead_index]

        mc_num = lead.get('MC Number')
        email_addr = lead.get('Email')
        
        if (mc_num and mc_num in removed_leads_set) or (email_addr and email_addr in removed_leads_set):
            print(f"Skipping lead {email_addr or mc_num} (in removed list).")
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

        try:
            message_id = message_id.strip().replace('<', '').replace('>', '')
            SentDripEmail.objects.create(
                drip_campaign=campaign,
                template=template,
                message_id=message_id,
                lead_email=lead['Email'],
                lead_mc_number=lead.get('MC Number')
            )
        except Exception as e:
            # Log this, but don't fail the send.
            print(f"CRITICAL: Failed to create SentDripEmail log: {e}")

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


@shared_task(name="drip_campaigns.send_batch_emails", acks_late=True, bind=True, default_retry_delay=300, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_batch_emails(self, campaign_id, account_info_id, template_id, start_index, batch_size):

    connection = None
    account = None
    template = None
    campaign = None
    email_account = None
    
    # This will be the index for the *next* batch task
    next_batch_start_index = start_index + batch_size
    daily_limit_hit = False

    try:
        template = DripTemplate.objects.get(id=template_id)
        account = EmailAccountAndLeads.objects.get(id=account_info_id)
        campaign = DripCampaign.objects.get(id=campaign_id)
        removed_leads_set = set(campaign.removed_mc_numbers)

        if campaign.status == 'Cancelled':
            print(f"Campaign {campaign.id} was cancelled. Stopping chain.")
            return

        # Check 2: Has this specific account chain been stopped?
        if account.status == 'Stopped':
            print(f"Account {account.id} was manually stopped. Stopping chain.")
            # telling the system that this account is done for its leads
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        # Check 3: Has the entire step been finished or skipped?
        if template.delivered_status in ('Sent', 'Failed', 'Cancelled'):
            print(f"Template {template.id} is finished or {template.delivered_status}. Stopping chain.")
            # We call reschedule_or_finalize to "clock out"
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        # --- Connection & Setup (Done ONCE) ---
        email_account = account.email_account
        decrypted_password = email_account.get_password()
        connection = get_email_connection(email_account, decrypted_password)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()

        # --- Batch Processing Loop ---
        for lead_index in range(start_index, start_index + batch_size):
            
            lead = None # Reset lead for each iteration

            # --- Lead Fetching & Filtering (Inside Loop) ---
            if lead_index >= len(account.leads_data):
                print(f"Lead index {lead_index} is out of bounds. Ending batch early.")
                break # Finished all leads for this account

            lead = account.leads_data[lead_index]
            
            mc_num = lead.get('MC Number')
            email_addr = lead.get('Email')
            
            if (mc_num and mc_num in removed_leads_set) or (email_addr and email_addr in removed_leads_set):
                print(f"Skipping lead {email_addr or mc_num} (in removed list).")
                continue # Skip to next lead in batch

            if not isinstance(lead, dict) or 'Email' not in lead or not re.fullmatch(email_regex, lead['Email']):
                print(f"Skipping invalid lead: {lead}.")
                continue # Skip to next lead in batch

            # --- Prepare & Send Email (Inside Loop) ---
            try:
                personalized_subject = personalize_template(template.subject, lead)
                personalized_body = personalize_template(template.body, lead)
                
                message_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
                DOMAIN = "https://dispatchskool.com"
                personalized_body = sanitize_email_html(personalized_body, DOMAIN)

                # --- Tracking ---
                # (Tracking pixel logic would go here, per-email)

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
                    
                print(f"Drip Task (Batch): Sent to {lead['Email']} via {account.email_account.email_address}")

                # --- Create Send Log ---
                try:
                    message_id = message_id.strip().replace('<', '').replace('>', '')
                    SentDripEmail.objects.create(
                        drip_campaign=campaign,
                        template=template,
                        message_id=message_id,
                        lead_email=lead['Email'],
                        lead_mc_number=lead.get('MC Number')
                    )
                except Exception as e:
                    print(f"CRITICAL: Failed to create SentDripEmail log: {e}")

                # --- Update Stats (Atomic) ---
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
            
            # --- Per-Email Error Handling (Inside Loop) ---
            except Exception as e:
                print(f"Failed to send to {lead['Email']} (Account {account_info_id}): {e}")
                error_message = str(e)
                
                if "Daily user sending limit exceeded" in error_message:
                    print(f"Daily limit exceeded for {email_account.email_address}. Halting batch for this account.")
                    daily_limit_hit = True
                    break # Exit the for loop

                elif "timeout" in error_message.lower() or "connection" in error_message.lower():
                    print(f"Network error on lead {lead['Email']}. Skipping lead.")
                    continue # Skip to next lead
                
                else:
                    print(f"Unhandled error for {lead['Email']}: {e}. Skipping lead.")
                    continue # Skip to next lead

            # --- Add Delay Between Emails ---
            email_delay = random.randint(campaign.min_delay, campaign.max_delay)
            time.sleep(email_delay)

    # --- Task-Level Error Handling (Outside Loop) ---
    except TimeLimitExceeded as e:
        print(f"Time limit exceeded for batch task (Account {account_info_id}): {e}. Rescheduling next batch.")
        reschedule_or_finalize(campaign.id, account, template, next_batch_start_index, 
                              delay_seconds=60, use_batch=True, batch_size=batch_size)
        return

    except (DripTemplate.DoesNotExist, EmailAccountAndLeads.DoesNotExist, DripCampaign.DoesNotExist) as e:
        print(f"Critical error: {e}. Stopping chain for account {account_info_id}.")
        return

    except Exception as e:
        print(f"Failed to send batch starting at {start_index} (Account {account_info_id}): {e}")
        error_message = str(e)
        
        # This is for errors *outside* the loop (e.g., initial connection)
        if "timeout" in error_message.lower() or "connection" in error_message.lower():
            print(f"Network error for account {account_info_id}. Retrying task.")
            raise self.retry(exc=e, max_retries=3) 
        
        else:
            print(f"Unhandled error for batch: {e}. Skipping batch.")
            reschedule_or_finalize(campaign.id, account, template, next_batch_start_index, 
                                  delay_seconds=1, use_batch=True, batch_size=batch_size)
            return

    # --- CLEANUP (Outside Loop) ---
    finally:
        if connection:
            connection.close()

    # --- RESCHEDULE (Outside Loop) ---
    if daily_limit_hit:
        # Force finalization for this account
        print(f"Halting account {account.id} due to daily limit.")
        reschedule_or_finalize(campaign.id, account, template, account.recipient_count, 
                              delay_seconds=1, use_batch=True, batch_size=batch_size)
    else:
        # Schedule the next batch.
        reschedule_or_finalize(campaign.id, account, template, next_batch_start_index, 
                              delay_seconds=random.randint(1, 5), use_batch=True, batch_size=batch_size)


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

        if template.delivered_status == 'Cancelled':
            print(f"Finalizer: Step {template.step_number} was 'Cancelled' (Skipped).")
            # Do NOT set to 'Sent'. Just proceed.
        else:
            template.delivered_status = 'Sent'
            template.save(update_fields=['delivered_status'])

        # Set all 'Processing' accounts back to 'Ready'
        EmailAccountAndLeads.objects.filter(
            campaign=campaign,
            status='Processing'
        ).update(status='Ready')
        
        # Find the *next available step* with a number
        # greater than the one we just finished.
        
        next_step_template = DripTemplate.objects.filter(
            campaign=campaign, 
            step_number__gt=campaign.current_step
        ).order_by('step_number').first() # Get the very next one
        
        if next_step_template:
            
            # Calculate its run time according to last_action, so it's relative to last_action not relative to "completion of last action"
            new_next_action_at = campaign.last_action_at + campaign.step_delay
            
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

            # --- NEW: Set all 'Ready' accounts to 'Completed' ---
            EmailAccountAndLeads.objects.filter(
                campaign=campaign,
                status='Ready'
            ).update(status='Completed')
            
            print(f"Campaign {campaign.id} successfully completed.")
            return
            
    except Exception as e:
        print(f"Failed to finalize step for campaign {campaign_id}: {e}")
        DripCampaign.objects.filter(id=campaign_id).update(status='Failed')
        raise e


