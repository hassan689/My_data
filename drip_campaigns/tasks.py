from growth_skool.celery import app
from celery import shared_task, chord
from email.utils import make_msgid, formataddr
from celery.exceptions import TimeLimitExceeded, MaxRetriesExceededError
from urllib.parse import urljoin

from .models import DripCampaign, DripVariation, EmailAccountAndLeads, DripTemplate, SentDripEmail
from dashboard.models import GmailToken
from unibox.models import EmailThread, OutgoingEmailMessage

from dashboard.utilities import get_email_connection, personalize_template, sanitize_email_html, should_use_batch_processing, bake_lead_snapshot
from .utilities import reschedule_or_finalize, normalize_provider, send_campaign_failure_alert, IMAP_SETTINGS_MAP, get_imap_connection, save_email_with_existing_connection, get_best_sent_folder
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.encoding import force_str
from django.core.cache import cache
from django.conf import settings
from django.core.signing import TimestampSigner
from django.urls import reverse
from datetime import timedelta

import re
import random
import uuid
import imaplib
import email
import time


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
            print("Checking for replies!")
            check_campaign_replies_task.delay(campaign.id)
            
        except Exception as e:
            print(f"Error triggering campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])
            
            # --- ALERT CALL ---
            send_campaign_failure_alert(
                error_message=str(e),
                location="check_scheduled_drip_step (Cron)",
                campaign_id=campaign.id
            )

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
        
        # --- ALERT CALL ---
        send_campaign_failure_alert(
            error_message=str(e),
            location="check_campaign_replies_task (IMAP Coordinator)",
            campaign_id=campaign_id
        )
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
            try:
                account_info.last_reply_check_at = timezone.now()
                account_info.save(update_fields=['last_reply_check_at'])
            except Exception:
                pass
        try:
            raise self.retry(exc=e, max_retries=3)
        except MaxRetriesExceededError:
            # DO NOT RAISE. Return a string so the Chord continues.
            print(f"Max retries hit for IMAP check {account_info_id}. Skipping account.")
            return f"Failed: Max Retries for {account_info_id}"    
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
        removed_leads_set = set(
            item.lower() if isinstance(item, str) else item
            for item in campaign.removed_mc_numbers
        )

        sent_leads_set = set(
            email.lower()
            for email in SentDripEmail.objects.filter(
                drip_campaign=campaign,
                template=template
            ).values_list('lead_email', flat=True)
        )

        total_accounts_for_step = 0
        
        # 1. Loop 1: Set the 'goal' counts for each account
        for account_info in all_accounts:
            valid_leads = []
            for lead in account_info.leads_data:
                mc_num = lead.get('MC Number')
                raw_email = lead.get('Email')
                email_addr = raw_email.lower() if raw_email else None

                # Check if either identifier is in the "stop list" or "already sent to for this template" (used for resuming campaigns)
                if (mc_num and mc_num in removed_leads_set) or (email_addr and email_addr in removed_leads_set) or (email_addr and email_addr in sent_leads_set):
                    continue
                
                valid_leads.append(lead)
            
            account_info.recipient_count = len(valid_leads)
            account_info.filtered_leads = valid_leads
            account_info.sent_count = 0

            # Set the new status
            if len(valid_leads) > 0:
                account_info.status = 'Processing'
                total_accounts_for_step += 1
            else:
                account_info.status = 'Ready'
            
            account_info.save(update_fields=['filtered_leads', 'recipient_count', 'sent_count', 'status'])
        
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
        
        # --- ALERT CALL ---
        send_campaign_failure_alert(
            error_message=str(e),
            location="chain_starter_task (Dispatcher)",
            campaign_id=campaign_id
        )
        raise e


# ===================================================================
# TASK(S) 3: THE WORKERS
# ===================================================================
@shared_task(name="drip_campaigns.send_single_email", bind=True, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_single_email(self, campaign_id, account_info_id, template_id, lead_index):

    connection = None
    imap_connection = None
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
        if lead_index >= account.recipient_count:
            print(f"Lead index {lead_index} is out of bounds for recipient goal {account.recipient_count}. Stopping.")
            # This task is done, trigger the finalize check
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

        lead = account.filtered_leads[lead_index]

        mc_num = lead.get('MC Number')
        email_addr = lead.get('Email')

        # --- PRE-FLIGHT GUARD ---
        # We check the database BEFORE doing any connection work
        already_sent = SentDripEmail.objects.filter(
            drip_campaign=campaign,
            template=template,
            lead_email=email_addr.lower()
        ).exists()

        if already_sent:
            print(f"Skipping: Lead {email_addr} already processed for Step {template.step_number}")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return
        
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
        imap_connection = get_imap_connection(email_account)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()

        # --- Prepare Email ---
        # We use lead_index as the rotation counter (0, 1, 2...)
        variation = template.get_assigned_variation(lead_index) # will use the subject and body of the template (manager) for older camapigns with no var

        # Handle Legacy vs New Attribute names
        # DripVariation uses 'track_variation', DripTemplate (Legacy) uses 'track_template'
        should_track = getattr(variation, 'track_variation', getattr(variation, 'track_template', False))

        personalized_subject = personalize_template(variation.subject, lead)
        personalized_body = personalize_template(variation.body, lead)

        tracking_domain = email_account.effective_tracking_domain
        current_domain = tracking_domain if tracking_domain else 'dispatchskool.com'
        raw_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain=current_domain)
        clean_message_id = raw_msg_id.strip('<>')
        
        sanitization_base = f"https://{current_domain}"
        personalized_body = sanitize_email_html(personalized_body, sanitization_base)

        # --- Tracking ---
        unique_id = None
        if should_track and tracking_domain:
            
            unique_id = uuid.uuid4()
            pixel_url = reverse('drip_campaigns:track_drip', kwargs={'unique_identifier': unique_id})
            pixel_link = urljoin(f"https://{tracking_domain}", pixel_url)
            
            # tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
            # personalized_body += tracking_pixel

            tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
            if "</body>" in personalized_body:
                personalized_body = personalized_body.replace("</body>", f"{tracking_pixel}</body>")
            else:
                personalized_body += tracking_pixel

        # Check if DB flag is True AND if we have a valid tracking domain
        unsubscribe_url = None
        if getattr(variation, 'include_unsubscribe', False) and tracking_domain:
            signer = TimestampSigner()
            
            # Create the stateless token
            token = signer.sign_object({
                'uid': campaign.launched_by.id,
                'email': lead['Email']
            })
            
            # Construct the URL using the verified tracking domain
            clean_domain = tracking_domain.strip('/')
            unsubscribe_url = f"https://{clean_domain}/leads_data/unsubscribe/{token}/"
            
            # 1. Append HTML Footer (The visible link)
            footer_html = f"""
                <br><br>
                <span style="font-size: 11px; color: #888;">
                    Don't want to hear from me again? 
                    <a href="{unsubscribe_url}" style="color: #888;">Click here to unsubscribe</a>.
                </span>
            """
            
            if "</body>" in personalized_body:
                personalized_body = personalized_body.replace("</body>", f"{footer_html}</body>")
            else:
                personalized_body += footer_html
        
        # Check if display_name exists, otherwise just use the email address
        if email_account.display_name:
            from_email = formataddr((email_account.display_name, email_account.email_address))
        else:
            from_email = email_account.email_address
        
        
        try:
            with transaction.atomic():
                variation_fk = variation if isinstance(variation, DripVariation) else None
                lead_snapshot = bake_lead_snapshot(lead)

                # We lower() the email here to match the constraint strictly
                SentDripEmail.objects.create(
                    drip_campaign=campaign,
                    template=template,
                    variation=variation_fk,
                    message_id=clean_message_id,
                    lead_email=email_addr.lower(), 
                    lead_snapshot=lead_snapshot,
                    unique_identifier=unique_id,
                    lead_mc_number=lead.get('MC Number'),
                    status='Sent'
                )
        except IntegrityError:
            # IMPORTANT: Catch this so the chain doesn't die!
            print(f"Collision: {email_addr} grabbed by another worker. Moving to next.")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return
        
        # --- Send Email ---
        msg = EmailMultiAlternatives(
            subject=personalized_subject,
            body=strip_tags(personalized_body),
            from_email=from_email,
            to=[lead['Email']],
            connection=connection
        )
        # Base headers
        headers = {'Message-ID': raw_msg_id}
        
        # 2. Add List-Unsubscribe Headers (The "Magic" Button)
        if unsubscribe_url:
            headers['List-Unsubscribe'] = f"<{unsubscribe_url}>"
            headers['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
            
        msg.extra_headers = headers
        msg.attach_alternative(personalized_body, "text/html")
        
        try:
            msg.send()

            # --- Save to Sent Folder (IMAP) IF NOT ALREADY THERE ---
            if imap_connection:
                try:
                    raw_message = msg.message().as_bytes()
                    save_email_with_existing_connection(imap_connection, raw_message, raw_msg_id)
                except Exception as e:
                    print(f"IMAP Append failed, trying to reconnect... {e}")
                    # Simple Reconnect Logic
                    try:
                        imap_connection = get_imap_connection(email_account)
                        save_email_with_existing_connection(imap_connection, raw_message, raw_msg_id)
                    except:
                        print("IMAP Reconnect failed. Skipping save.")
            
        except Exception as e:
            if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower():
                print("SMTP connection lost, moving on ...")
                connection.close()
            
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
                message_id=raw_msg_id,
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

            try:
                send_mail(
                    subject=subject,
                    message=body_encoded,
                    from_email=from_email,
                    recipient_list=recipient_list,
                    fail_silently=False,
                )
            except:
                pass
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, delay_seconds=1)
            return

        elif "timeout" in error_message.lower() or "connection" in error_message.lower():
            print(f"Network error for account {account_info_id}. Retrying task.")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return
        
        else:
            print(f"Unhandled error for {lead['Email']}: {e}. Skipping lead.")
            reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=1)
            return

    # --- CLEANUP ---
    finally:
        if connection:
            connection.close()
        if imap_connection:
            try:
                imap_connection.logout()
            except:
                pass

    # --- RESCHEDULE ---
    next_delay = random.randint(campaign.min_delay, campaign.max_delay)
    reschedule_or_finalize(campaign.id, account, template, next_lead_index, delay_seconds=next_delay)


@shared_task(name="drip_campaigns.send_batch_emails", bind=True, time_limit=EMAIL_TASK_TIME_LIMIT)
def send_batch_emails(self, campaign_id, account_info_id, template_id, start_index, batch_size):

    connection = None
    imap_connection = None
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
        imap_connection = get_imap_connection(email_account)
        mailbox_instance = GmailToken.objects.filter(email_account=email_account).first()

        # OPTIMIZATION: Resolve 'Sent' Folder Name ONCE for the whole batch
        batch_folder_name = None
        if imap_connection:
            try:
                batch_folder_name = get_best_sent_folder(imap_connection)
            except:
                batch_folder_name = "Sent" # Fallback

        # --- Batch Processing Loop ---
        for lead_index in range(start_index, start_index + batch_size):
            
            lead = None # Reset lead for each iteration

            # --- Lead Fetching & Filtering (Inside Loop) ---
            if lead_index >= account.recipient_count:
                print(f"Lead index {lead_index} is out of bounds. Ending batch early.")
                break # Finished all leads for this account

            lead = account.filtered_leads[lead_index]
            
            mc_num = lead.get('MC Number')
            email_addr = lead.get('Email')

            if SentDripEmail.objects.filter(
                drip_campaign=campaign,
                template=template,
                lead_email=email_addr.lower()
            ).exists():
                continue
            
            if (mc_num and mc_num in removed_leads_set) or (email_addr and email_addr.lower() in removed_leads_set):
                print(f"Skipping lead {email_addr or mc_num} (in removed list).")
                continue # Skip to next lead in batch

            if not isinstance(lead, dict) or 'Email' not in lead or not re.fullmatch(email_regex, lead['Email']):
                print(f"Skipping invalid lead: {lead}.")
                continue # Skip to next lead in batch

            # --- Prepare & Send Email (Inside Loop) ---
            try:
                # NEW: Fetch variation using the current lead_index
                variation = template.get_assigned_variation(lead_index)
                
                # Handle Attribute differences
                should_track = getattr(variation, 'track_variation', getattr(variation, 'track_template', False))

                personalized_subject = personalize_template(variation.subject, lead)
                personalized_body = personalize_template(variation.body, lead)
                
                # 1. Set the Domain for Message-ID and Sanitization
                tracking_domain = email_account.effective_tracking_domain
                current_domain = tracking_domain if tracking_domain else 'dispatchskool.com'

                raw_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain=current_domain)
                clean_message_id = raw_msg_id.strip('<>')
                
                sanitization_base = f"https://{current_domain}"
                personalized_body = sanitize_email_html(personalized_body, sanitization_base)
                current_snapshot = bake_lead_snapshot(lead)

                # --- Tracking ---
                unique_id = None
                if should_track and tracking_domain:

                    unique_id = uuid.uuid4()
                    pixel_url = reverse('drip_campaigns:track_drip', kwargs={'unique_identifier': unique_id})
                    pixel_link = urljoin(f"https://{tracking_domain}", pixel_url)
                    
                    # tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
                    # personalized_body += tracking_pixel

                    tracking_pixel = f'<img src="{pixel_link}" width="1" height="1" style="display:none;" alt="">'
                    if "</body>" in personalized_body:
                        personalized_body = personalized_body.replace("</body>", f"{tracking_pixel}</body>")
                    else:
                        personalized_body += tracking_pixel

                # Check if DB flag is True AND if we have a valid tracking domain
                unsubscribe_url = None
                if getattr(variation, 'include_unsubscribe', False) and tracking_domain:
                    signer = TimestampSigner()
                    
                    # Create the stateless token
                    token = signer.sign_object({
                        'uid': campaign.launched_by.id,
                        'email': lead['Email']
                    })
                    
                    # Construct the URL using the verified tracking domain
                    clean_domain = tracking_domain.strip('/')
                    unsubscribe_url = f"https://{clean_domain}/leads_data/unsubscribe/{token}/"
                    
                    # 1. Append HTML Footer (The visible link)
                    footer_html = f"""
                        <br><br>
                        <span style="font-size: 11px; color: #888;">
                            Don't want to hear from me again? 
                            <a href="{unsubscribe_url}" style="color: #888;">Click here to unsubscribe</a>.
                        </span>
                    """
                    
                    if "</body>" in personalized_body:
                        personalized_body = personalized_body.replace("</body>", f"{footer_html}</body>")
                    else:
                        personalized_body += footer_html
                
                # Check if display_name exists, otherwise just use the email address
                if email_account.display_name:
                    from_email = formataddr((email_account.display_name, email_account.email_address))
                else:
                    from_email = email_account.email_address
                
                try:
                    with transaction.atomic():
                        variation_fk = variation if isinstance(variation, DripVariation) else None
                        SentDripEmail.objects.create(
                            drip_campaign=campaign,
                            template=template,
                            variation=variation_fk,
                            message_id=clean_message_id,
                            lead_email=email_addr.lower(),
                            lead_snapshot=current_snapshot,
                            unique_identifier=unique_id,
                            lead_mc_number=lead.get('MC Number'),
                            status='Sent'
                        )
                except IntegrityError:
                    print(f"Batch Collision: {email_addr} already logged. Skipping.")
                    continue

                # --- Send Email ---
                msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=strip_tags(personalized_body),
                    from_email=from_email,
                    to=[lead['Email']],
                    connection=connection
                )
                # Base headers
                headers = {'Message-ID': raw_msg_id}
                
                # 2. Add List-Unsubscribe Headers (The "Magic" Button)
                if unsubscribe_url:
                    headers['List-Unsubscribe'] = f"<{unsubscribe_url}>"
                    headers['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
                    
                msg.extra_headers = headers
                msg.attach_alternative(personalized_body, "text/html")
                
                try:
                    msg.send()

                    # --- Save to Sent Folder (IMAP) IF NOT ALREADY THERE ---
                    if imap_connection:
                        try:
                            raw_message = msg.message().as_bytes()
                            save_email_with_existing_connection(imap_connection, raw_message, raw_msg_id, cached_folder_name=batch_folder_name)
                        except Exception as e:
                            print(f"IMAP Append failed, trying to reconnect... {e}")
                            # Simple Reconnect Logic
                            try:
                                imap_connection = get_imap_connection(email_account)
                                save_email_with_existing_connection(imap_connection, raw_message, raw_msg_id, cached_folder_name=batch_folder_name)
                            except:
                                print("IMAP Reconnect failed. Skipping save.")
                    
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower():
                        print("SMTP connection lost, moving on ...")
                        connection.close()

                print(f"Drip Task: Sent to {lead['Email']} via {account.email_account.email_address}")

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
                        message_id=raw_msg_id,
                        in_reply_to=None,
                    )
            
            # --- Per-Email Error Handling (Inside Loop) ---
            except Exception as e:
                print(f"Failed to send to {lead['Email']} (Account {account_info_id}): {e}")
                error_message = str(e)
                
                if "Daily user sending limit exceeded" in error_message:
                    print(f"Daily limit exceeded for {email_account.email_address}. Halting batch for this account.")
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

                    try:
                        send_mail(
                            subject=subject,
                            message=body_encoded,
                            from_email=from_email,
                            recipient_list=recipient_list,
                            fail_silently=False,
                        )
                    except:
                        pass
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
            reschedule_or_finalize(campaign.id, account, template, account.recipient_count, 
                                      delay_seconds=1, use_batch=True, batch_size=batch_size)
            return
        
        else:
            print(f"Unhandled error for batch: {e}. Skipping batch.")
            reschedule_or_finalize(campaign.id, account, template, next_batch_start_index, 
                                  delay_seconds=1, use_batch=True, batch_size=batch_size)
            return

    # --- CLEANUP (Outside Loop) ---
    finally:
        if connection:
            connection.close()
        if imap_connection:
            try:
                imap_connection.logout()
            except:
                pass

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
        
        # --- ALERT CALL ---
        send_campaign_failure_alert(
            error_message=str(e),
            location="finalize_drip_step_task (Finalizer)",
            campaign_id=campaign_id
        )
        raise e


# ===================================================================
# TASK 5: CLEANUP OLD CAMPAIGNS AND DATA
# ===================================================================

@app.task(name="drip_campaigns.tasks.clear_drip_campaigns")
def clear_drip_campaigns():
    """
    Deletes DripCampaign entries where:
    - status is 'Completed', 'Failed' or 'Cancelled' AND
    - created_at is older than 365 days.
    
    Because of on_delete=models.CASCADE in your models, this will automatically
    wipe the associated:
    - EmailAccountAndLeads
    - DripTemplates
    - SentDripEmail (the logs)
    """
    cutoff_date = timezone.now() - timedelta(days=365)
    safe_to_delete_statuses = ['Completed', 'Failed', 'Cancelled']
    
    deleted_count, _ = DripCampaign.objects.filter(
        status__in=safe_to_delete_statuses,
        created_at__lt=cutoff_date
    ).delete()

    return f"Deleted {deleted_count} old drip campaigns and related data."

