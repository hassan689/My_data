import random
from django.utils import timezone
from .models import WarmupCampaign, WarmupMessage
from users.models import EmailAccount
from growth_skool.celery import app
from django.core.mail import EmailMultiAlternatives, send_mail, EmailMessage
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_str
import time
from django.db import connections
from celery.exceptions import SoftTimeLimitExceeded
from .utilities import *
from email.utils import formataddr, make_msgid
import uuid
import requests
from django.db.models import Q
from celery import shared_task, chord
from django.core.cache import cache
from .utilities import process_audit_results, generate_spintax_body, generate_spintax_subject, get_humanized_delay


# @app.task(name="warmup.tasks.send_warmup_step", soft_time_limit=600, time_limit=700)
def send_warmup_step(campaign_id, step_number):
    """
    Sends the next step of a warmup conversation for a given campaign.
    """
    try:
        try:
            campaign = WarmupCampaign.objects.select_related(
                'sender_account'
            ).prefetch_related(
                'target_accounts'
            ).get(id=campaign_id)
        except WarmupCampaign.DoesNotExist:
            print(f"Warmup campaign with ID {campaign_id} not found.")
            return

        # Check if the campaign has already completed or failed
        if campaign.status in ['Complete', 'Failed']:
            print(f"Campaign {campaign.id} is already in {campaign.status} state. Skipping.")
            return
        
        # Update campaign status for the next step (even if it fails, it receives emails in the target's trunk, so we must advance the step to keep the logic consistent)
        campaign.current_step += 1
        campaign.last_action_at = timezone.now()
        campaign.next_action_at = get_humanized_delay()
        
        campaign.save(update_fields=['current_step', 'last_action_at', 'next_action_at'])
        
        # --- NEW LOGIC: Refresh targets every 6 steps (Start of a new conversation cycle) ---
        is_new_cycle = (step_number % 6 == 0)
        msg = f"--- [START] Processing Campaign {campaign.id} | Step {step_number} | New Cycle: {is_new_cycle}"
        print(msg)

        if is_new_cycle:
            msg = f"--- [REFRESH] Step {step_number} triggered target refresh for {campaign.sender_account.email_address}"
            print(msg)
            # If step 0, we MUST have targets. If step 4, 8, etc., we refresh them.
            selected_targets = refresh_targets(campaign)
            
            # CRITICAL: If the pool is empty, refresh_targets already handled 
            # the campaign.next_action_at update. We just exit here.
            if not selected_targets:
                msg = f"--- [POSTPONE] Campaign {campaign.id} postponed: Target pool exhausted."
                print(msg)
                return 

        # Now get the targets for the rest of the task logic
        recipients = list(campaign.target_accounts.all())
        
        # Safety check: if for some reason targets are missing and it wasn't a refresh step
        if not recipients:
            msg = f"--- [RECOVERY] No targets found for Campaign {campaign.id}. Attempting refresh..."
            print(msg)
            refresh_targets(campaign)
            return
        
        # ==============================================================================
        # SENDER'S TURN (Even steps: 0, 2, 4...)
        # ==============================================================================
        if step_number % 2 == 0:
            try:
                sender_account = campaign.sender_account
                msg = f"--- [CONNECT] Attempting SMTP Connection for Sender: {sender_account.email_address}"
                print(msg)
                
                recipients = list(campaign.target_accounts.all())
                decrypted_password = sender_account.get_password()
                connection = get_email_connection(sender_account, decrypted_password)
                
                # CRITICAL: If the connection itself fails, we must exit and reschedule.
                if not connection:
                    msg = f"--- [FAILURE] Could not establish SMTP for {sender_account.email_address}. Rescheduling..."
                    print(msg)
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                    campaign.save(update_fields=["next_action_at"])
                    return  # Early exit only on total connection failure
                
                sent_successfully = False
                for recipient_account in recipients:
                    try:
                        msg = f"--- [SENDING] {sender_account.email_address} -> {recipient_account.email_address} (Step {step_number})"
                        print(msg)
                        
                        # --- THREADING & CONTENT LOGIC ---
                        new_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
                        thread_id = uuid.uuid4()
                        parent_message_id = None
                        is_reply = False
                        quoted_body = None

                        if not is_new_cycle and step_number > 0:
                            last_msg = WarmupMessage.objects.filter(
                                campaign=campaign,
                                sender=recipient_account,
                                recipient=sender_account
                            ).order_by('-sent_at').first()

                            if last_msg:
                                imap_conn = get_warmup_imap_connection(sender_account)
                                if imap_conn:
                                    try:
                                        quoted_body = check_inbox_and_rescue(imap_conn, last_msg.message_id)
                                    finally:
                                        try: imap_conn.logout() 
                                        except: pass
                                
                                if quoted_body:
                                    is_reply = True
                                    parent_message_id = last_msg.message_id
                                    thread_id = last_msg.thread_id

                        if is_reply:
                            subject_raw = last_msg.subject
                            # If it already has Re:, keep it. If not, add it.
                            personalized_subject = subject_raw if subject_raw.lower().startswith("re:") else f"Re: {subject_raw}"
                            fresh_body = generate_spintax_body(recipient_account.user.first_name, getattr(recipient_account.user, "company_name", "ABC Transports LLC"), step_number)
                            personalized_body = f"{fresh_body}\n\nOn {last_msg.sent_at.strftime('%a, %b %d, %Y')}, {recipient_account.email_address} wrote:\n> {quoted_body.replace(chr(10), chr(10)+'> ')}"
                        else:
                            # Step 0 or Thread Broken
                            personalized_subject = generate_spintax_subject(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports"))
                            personalized_body = generate_spintax_body(recipient_account.user.first_name, getattr(recipient_account.user, "company_name", "ABC Transports LLC"), step_number)

                        # --- PREPARE MESSAGE ---
                        from_email = formataddr((sender_account.display_name, sender_account.email_address)) if sender_account.display_name else sender_account.email_address
                        
                        main_msg = EmailMultiAlternatives(
                            subject=personalized_subject,
                            body=personalized_body,
                            from_email=from_email,
                            to=[recipient_account.email_address],
                            connection=connection
                        )
                        main_msg.extra_headers = {'Message-ID': new_msg_id}
                        if is_reply:
                            main_msg.extra_headers['In-Reply-To'] = parent_message_id
                            main_msg.extra_headers['References'] = parent_message_id
                        main_msg.encoding = 'utf-8'

                        # --- ACTUAL SEND ---
                        main_msg.send()
                        sent_successfully = True
                        
                        WarmupMessage.objects.create(
                            campaign=campaign,
                            sender=sender_account,
                            recipient=recipient_account,
                            subject=personalized_subject,
                            body=personalized_body,
                            message_id=new_msg_id,
                            in_reply_to_id=parent_message_id,
                            thread_id=thread_id
                        )

                        connections.close_all()
                        time.sleep(random.randint(10, 40))

                    except Exception as e:
                        # If one recipient fails, log it and CONTINUE to the next recipient
                        msg = f"--- [RETRYABLE ERROR] {sender_account.email_address} -> {recipient_account.email_address}: {str(e)}"
                        print(msg)
                        
                        # Handle specific connection drops inside the loop
                        if any(err in str(e) for err in ["connect() first", "expired", "unexpectedly closed", "reset by peer"]):
                            connection = get_email_connection(sender_account, decrypted_password)
                            # If reconnection fails, we break the recipient loop
                            if not connection: break
                        
                        continue # Don't return, try the next peer

                if connection:
                    connection.close()
                
                # If we failed to send to EVERYONE, return early to avoid advancing the step
                if not sent_successfully:
                    return

            except Exception as e:
                
                if "Username and Password not accepted" in str(e):
                    campaign.status = 'Failed'
                    campaign.save(update_fields=['status'])
                    sender_account.is_warmup_target = False 
                    sender_account.save(update_fields=['is_warmup_target'])
                    subject = "Email account configuration failure"
                    body = (
                        f"Hello {sender_account.user.first_name},\n\n"
                        f"Error during email attach: {e}\n\n"
                        f"This is to notify you that your email account {sender_account.email_address} "
                        "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                        "In case of any problems, feel free to reach out.\n\n"
                        "Best Regards,\nThe Dispatch Skool Team."
                    )
                    from_email = settings.EMAIL_HOST_USER
                    recipient_list = [sender_account.user.email]
                    body_encoded = force_str(body, 'utf-8', errors='replace')
                    email_message = EmailMessage(subject, body_encoded, from_email, recipient_list)
                    email_message.send()
                
                elif "Daily user sending limit exceeded" in str(e):
                    campaign.next_action_at = get_humanized_delay()
                    campaign.save(update_fields=['next_action_at'])

                elif "codec can't encode character" in str(e): 
                    # Simplify fallback for encoding errors (non-threaded)
                    personalized_subject = generate_spintax_subject(
                        recipient_first_name=recipient_account.user.first_name,
                        sender_company_name=getattr(sender_account.user, "company_name", "ABC Transports")
                    )
                    personalized_body = generate_spintax_body(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports LLC"), step_number)
                    main_msg = EmailMultiAlternatives(subject=personalized_subject, body=personalized_body, from_email=sender_account.email_address, to=[recipient_account.email_address], connection=connection)
                    main_msg.encoding = 'utf-8'
                    try:
                        main_msg.send()
                    except Exception as e:
                        # Retry logic matching your block
                        if "please run connect() first" in str(e) or "connection expired" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection reset by peer" in str(e):
                            connection = get_email_connection(sender_account, decrypted_password)
                            if not connection:
                                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                                campaign.save(update_fields=["next_action_at"])

                            main_msg.connection = connection
                            main_msg.send()
                        else:
                            campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                            campaign.save(update_fields=['next_action_at'])

                elif "Connection unexpectedly closed" in str(e) or "too many AUTH commands" in str(e) or "Connection timed out" in str(e) or "Server busy" in str(e) or "Server not connected" in str(e) or "timeout exceeded" in str(e) or "Connection reset by peer" in str(e):
                    connection = get_email_connection(sender_account, decrypted_password)
                    if not connection:
                        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                        campaign.save(update_fields=["next_action_at"])
                    try:
                        main_msg.connection = connection
                        try:
                            main_msg.send()
                            
                            # Save to DB
                            WarmupMessage.objects.create(
                                campaign=campaign,
                                sender=sender_account,
                                recipient=recipient_account,
                                subject=personalized_subject,
                                body=personalized_body,
                                message_id=new_msg_id,
                                in_reply_to_id=parent_message_id,
                                thread_id=thread_id
                            )
                        except Exception as e:
                            campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                            campaign.save(update_fields=['next_action_at'])

                    except:
                        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                        campaign.save(update_fields=['next_action_at'])

                elif "Temporary System Problem" in str(e) or "Concurrent connections limit exceeded" in str(e):
                    campaign.next_action_at = get_humanized_delay()
                    campaign.save(update_fields=['next_action_at'])

                elif "Please log in with your web browser" in str(e) or "Sender address rejected" in str(e): 
                    EmailAccount.objects.filter(email_address=sender_account.email_address).update(is_warmup_target=False, black_list=True)
                
                else: 
                    subject = f"Error during Sender's Turn for Warmup Campaign"
                    body = f"Error during sender's turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                    recipient_list = ['abdullahatif132@gmail.com']
                    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipient_list, fail_silently=False)
                    campaign.next_action_at = get_humanized_delay()
                    campaign.save(update_fields=['next_action_at'])

                return
        
        # ==============================================================================
        # TARGETS' TURN (Odd steps: 1, 3, 5...)
        # ==============================================================================
        else:
            sender_accounts = list(campaign.target_accounts.all()) # These are the Targets acting as Senders now
            recipient_account = campaign.sender_account # The Campaign Sender is now the Recipient
            
            for sender_account in sender_accounts:
                
                msg = f"--- [CONNECT] Target acting as Sender: {sender_account.email_address}"
                print(msg)
                
                try: 
                    decrypted_password = sender_account.get_password()
                    connection = get_email_connection(sender_account, decrypted_password)
                    if not connection:
                        msg = f"--- [SKIP] Target {sender_account.email_address} connection failed."
                        print(msg)
                        continue
                    
                    # --- THREADING LOGIC ---
                    thread_id = None
                    parent_message_id = None
                    quoted_body = None
                    new_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
                    is_reply = False

                    # Find the message the Campaign Sender sent to THIS Target in the previous step
                    last_msg = WarmupMessage.objects.filter(
                        campaign=campaign,
                        sender=recipient_account, # The Campaign Sender
                        recipient=sender_account  # This Target
                    ).order_by('-sent_at').first()

                    if last_msg:
                        imap_conn = get_warmup_imap_connection(sender_account)
                        # Target checks their inbox for the Campaign Sender's email
                        if imap_conn:
                            try:
                                quoted_body = check_inbox_and_rescue(imap_conn, last_msg.message_id)
                            finally:
                                try: imap_conn.logout() 
                                except: pass
                        
                        if quoted_body:
                            is_reply = True
                            parent_message_id = last_msg.message_id
                            thread_id = last_msg.thread_id
                        else:
                            # Lost chain, fallback to fresh
                            thread_id = uuid.uuid4()
                    else:
                        thread_id = uuid.uuid4()

                    # --- CONTENT GENERATION ---
                    if is_reply:
                        subject_raw = last_msg.subject
                        # If it already has Re:, keep it. If not, add it.
                        personalized_subject = subject_raw if subject_raw.lower().startswith("re:") else f"Re: {subject_raw}"
                        fresh_body = generate_spintax_body(recipient_account.user.first_name, getattr(recipient_account.user, "company_name", "ABC Transports LLC"), step_number)
                        personalized_body = f"{fresh_body}\n\nOn {last_msg.sent_at.strftime('%a, %b %d, %Y')}, {recipient_account.email_address} wrote:\n> {quoted_body.replace(chr(10), chr(10)+'> ')}"
                    else:
                        # Step 0 or Thread Broken
                        personalized_subject = generate_spintax_subject(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports"))
                        personalized_body = generate_spintax_body(recipient_account.user.first_name, getattr(recipient_account.user, "company_name", "ABC Transports LLC"), step_number)
                    
                    # Check if display_name exists, otherwise just use the email address
                    if sender_account.display_name:
                        from_email = formataddr((sender_account.display_name, sender_account.email_address))
                    else:
                        from_email = sender_account.email_address

                    # --- SENDING ---
                    main_msg = EmailMultiAlternatives(
                        subject=personalized_subject,
                        body=personalized_body,
                        from_email=from_email,
                        to=[recipient_account.email_address],
                        connection=connection
                    )
                    
                    # Inject Headers
                    main_msg.extra_headers = {'Message-ID': new_msg_id}
                    if is_reply:
                        main_msg.extra_headers['In-Reply-To'] = parent_message_id
                        main_msg.extra_headers['References'] = parent_message_id

                    main_msg.encoding = 'utf-8'
                    try:
                        main_msg.send()

                        # Save to DB
                        WarmupMessage.objects.create(
                            campaign=campaign,
                            sender=sender_account,
                            recipient=recipient_account,
                            subject=personalized_subject,
                            body=personalized_body,
                            message_id=new_msg_id,
                            in_reply_to_id=parent_message_id,
                            thread_id=thread_id
                        )

                    except Exception as e:
                        if "please run connect() first" in str(e) or "connection expired" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection reset by peer" in str(e) or "Disabled by user from hPanel" in str(e):
                            print("SMTP connection lost, reconnecting...")
                            connection = get_email_connection(sender_account, decrypted_password)
                            if not connection:
                                continue
                            main_msg.connection = connection
                            try:
                                main_msg.send()
                                
                                # Save to DB
                                WarmupMessage.objects.create(
                                    campaign=campaign,
                                    sender=sender_account,
                                    recipient=recipient_account,
                                    subject=personalized_subject,
                                    body=personalized_body,
                                    message_id=new_msg_id,
                                    in_reply_to_id=parent_message_id,
                                    thread_id=thread_id
                                )
                            except Exception as e:
                                pass
                        else:
                            raise e
                        
                    connections.close_all() # open db connections

                    if connection: # smtp connections
                        connection.close()

                except Exception as e:
                    if "Connection reset by peer" in str(e) or "too many AUTH commands" in str(e) or "Disabled by user from hPanel" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection timed out" in str(e):
                        connection = get_email_connection(sender_account, decrypted_password)
                        if not connection:
                            continue
                        try:
                            main_msg.connection = connection
                            main_msg.send()
                        except:
                            continue

                    elif "codec can't encode character" in str(e): 
                        continue
                    
                    elif "Please log in with your web browser" in str(e) or "Sender address rejected" in str(e): 
                        EmailAccount.objects.filter(email_address=sender_account.email_address).update(is_warmup_target=False, black_list=True)
                        continue
                        
                    elif "Daily user sending limit exceeded" in str(e):
                        continue

                    elif "Username and Password not accepted" in str(e) or "authentication failed" in str(e):
                        sender_account.is_warmup_target = False 
                        sender_account.save(update_fields=['is_warmup_target'])
                        subject = "Email account configuration failure"
                        body = (
                            f"Hello {sender_account.user.first_name},\n\n"
                            f"Error during email attach: {e}\n\n"
                            f"This is to notify you that your email account {sender_account.email_address} "
                            "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                            "In case of any problems, feel free to reach out.\n\n"
                            "Best Regards,\nThe Dispatch Skool Team."
                        )
                        from_email = settings.EMAIL_HOST_USER
                        recipient_list = [sender_account.user.email]
                        body_encoded = force_str(body, 'utf-8', errors='replace')
                        email_message = EmailMessage(subject, body_encoded, from_email, recipient_list)
                        email_message.send()
                        continue

                    else:
                        subject = f"Error during Targets' Turn for Warmup Campaign"
                        body = f"Error during targets' turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                        recipient_list = ['abdullahatif132@gmail.com']
                        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipient_list, fail_silently=False)
                        continue


        msg = f"--- [FINISH] Campaign {campaign.id} advanced to Step {campaign.current_step}"
        print(msg)

    except SoftTimeLimitExceeded:
        print(f"[TIMEOUT] Warmup step {step_number} exceeded time limit — safely rescheduling.")
        WarmupCampaign.objects.filter(id=campaign_id).update(
            next_action_at=timezone.now() + timedelta(hours=random.uniform(1, 3))
        )
        connections.close_all()
        return


# @app.task(name="warmup.tasks.process_warmup_convo_beats")
def process_warmup_convo_beats():
    """
    Celery Beat task that checks for active warmup campaigns due for their next step.
    """
    now_utc = timezone.now()
    
    # Find active campaigns where next_action_at is due or last_action_at is null (initial step)
    campaigns_to_process = WarmupCampaign.objects.filter(
        status='Active',
        next_action_at__lte=now_utc
    )

    if not campaigns_to_process.exists():
        print("No warmup campaigns found to process.")
        return

    print(f"\nFound {campaigns_to_process.count()} warmup campaigns to process.\n")
    for campaign in campaigns_to_process:
        try:
            # Trigger the send_warmup_step task for the current step
            send_warmup_step.delay(campaign.id, campaign.current_step)
            print(f"\nTriggered send_warmup_step for WarmupCampaign {campaign.id}, Step {campaign.current_step}.\n")
            
        except Exception as e:
            # Log any errors that occur during the launching process
            print(f"Error triggering send_warmup_step for Campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])


# beat task to clear out warmup messages older than 7 days
# @app.task(name="warmup.tasks.clear_old_warmup_messages")
def clear_old_warmup_messages():
    cutoff_date = timezone.now() - timedelta(days=7)
    WarmupMessage.objects.filter(sent_at__lt=cutoff_date).delete()


# A robust Celery task using your Mails.so API to proactively blacklist bad accounts.

AUDIT_API_SUBMIT_URL = 'https://api.mails.so/v1/batch'
AUDIT_CHUNK_SIZE = 500
AUDIT_POLL_INTERVAL = 60
AUDIT_HARD_DEADLINE = 300


# @app.task(name="warmup.tasks.audit_warmup_targets")
def audit_warmup_targets():
    """
    Beat Task: Runs periodically (e.g., 4 times a day).
    Identifies all 'active' email accounts currently enabled for warmup
    and dispatches them to Mails.so to verify deliverability.
    """
    # Filter: Accounts that are marked for warmup, not yet blacklisted, only gmail accounts
    # belonging to users who have an Active Subscription OR are on a Free Trial.
    candidates = EmailAccount.objects.filter(
        Q(user__subscription__status='active') | Q(user__on_free_trial=True),
        email_address__endswith='@gmail.com',
        is_warmup_target=True,
        black_list=False
    ).distinct().values_list('email_address', flat=True)

    candidate_list = list(candidates)
    
    if not candidate_list:
        print("No warmup candidates found to audit.")
        return

    print(f"Auditing {len(candidate_list)} warmup accounts via Mails.so...")

    # Chunking
    for i in range(0, len(candidate_list), AUDIT_CHUNK_SIZE):
        chunk = candidate_list[i:i + AUDIT_CHUNK_SIZE]
        # Dispatch worker task for this chunk
        verify_warmup_batch.delay(chunk, time.time())


# @app.task(bind=True, max_retries=10, name="warmup.tasks.verify_warmup_batch")
def verify_warmup_batch(self, email_list, start_time, job_id=None, processed_map=None):
    """
    Worker Task: Submits a chunk of emails to Mails.so, polls for results,
    and updates the EmailAccount model directly.
    """
    elapsed = time.time() - start_time
    processed_map = processed_map or {}
    API_KEY = getattr(settings, 'MAILS_SO_API_KEY', '')
    headers = {'Content-Type': 'application/json', 'x-mails-api-key': API_KEY}

    # 1. HARD STOP
    if elapsed >= AUDIT_HARD_DEADLINE:
        print(f"Audit batch timed out. Processed {len(processed_map)}/{len(email_list)}")
        process_audit_results(processed_map)
        return

    # 2. SUBMISSION PHASE
    if not job_id:
        pending_emails = [
            e.strip().lower() for e in email_list 
            if e.strip().lower() not in processed_map
        ]
        
        if not pending_emails:
            process_audit_results(processed_map)
            return

        try:
            resp = requests.post(AUDIT_API_SUBMIT_URL, headers=headers, json={'emails': pending_emails}, timeout=30)
            if resp.status_code in (200, 201, 202):
                job_id = resp.json().get('id')
            else:
                raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': None, 'processed_map': processed_map})
        except Exception as e:
            print(f"API Submission Error: {e}")
            raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': None, 'processed_map': processed_map})

    # 3. POLLING PHASE
    try:
        poll_resp = requests.get(f"{AUDIT_API_SUBMIT_URL}/{job_id}", headers=headers, timeout=30)
        
        if poll_resp.status_code == 200:
            data = poll_resp.json()
            
            # Update local map
            for r in data.get('emails', []):
                email = str(r.get('email', '')).lower()
                processed_map[email] = r

            # Check completion
            if data.get('status') == 'completed' and len(processed_map) >= len(email_list):
                process_audit_results(processed_map)
                return

        # Soft Deadline Chase
        if elapsed >= (AUDIT_HARD_DEADLINE - 120) and len(processed_map) < len(email_list):
            job_id = None # Force resubmission of missing ones

    except Exception:
        pass 

    raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': job_id, 'processed_map': processed_map})



# @shared_task(name="warmup.tasks.reputation_guard_orchestrator")
def orchestrate_reputation_guard(cache_key=None, current_index=0):
    BATCH_SIZE = 10

    if cache_key is None:
        # 1. Initial run: Fetch IDs
        account_ids = list(EmailAccount.objects.filter(is_warmup_target=True).values_list('id', flat=True))
        if not account_ids: return "No accounts to process"
        
        # 2. Store list in cache instead of passing it via arguments
        cache_key = f"warmup_rescue_list_{uuid.uuid4().hex}"
        cache.set(cache_key, account_ids, timeout=36000) # 10hr expiry
    else:
        account_ids = cache.get(cache_key)
    
    if not account_ids: return "Cache expired or empty"

    batch_ids = account_ids[current_index : current_index + BATCH_SIZE]
    remaining_index = current_index + BATCH_SIZE
    
    if not batch_ids:
        cache.delete(cache_key) # Clean up
        return "Cycle Complete"

    # Chord Header: Parallel Workers
    # Chord Body: Callback handles the next batch
    header = [rescue_worker_task.s(acc_id) for acc_id in batch_ids]
    callback = batch_complete_callback.s(cache_key, remaining_index)
    
    return chord(header)(callback)


# @shared_task(name="warmup.tasks.rescue_worker_task")
def rescue_worker_task(account_id):
    try:
        account = EmailAccount.objects.get(id=account_id)
        
        # Find all messages sent TO this account in the last 5 days
        cutoff = timezone.now() - timedelta(hours=120)
        messages_to_rescue = WarmupMessage.objects.filter(
            recipient=account,
            sent_at__gte=cutoff
        ).values_list('message_id', flat=True)

        if not messages_to_rescue:
            return f"Success: {account.email_address} (0 messages to rescue)"

        # Open connection ONCE per worker task
        imap_conn = get_warmup_imap_connection(account)
        if not imap_conn:
            return f"Failed: {account.email_address} - IMAP connection failed"
        
        # Use the utility to crawl and move
        count = 0
        try:
            for msg_id in messages_to_rescue:
                result = check_inbox_and_rescue(imap_conn, msg_id)
                if result:
                    count += 1
        finally:
            # Tear down connection after the loop
            try:
                imap_conn.logout()
            except:
                pass
        
        return f"Success: {account.email_address} ({count} rescued)"

    except Exception as e:
        # Move on if login fails or account doesn't exist
        return f"Failed: Account {account_id} - {str(e)}"


# @shared_task(name="warmup.tasks.batch_complete_callback")
def batch_complete_callback(worker_results, cache_key, next_index):
    """
    Fired after a batch of 10 workers finish.
    """
    print(f"Batch Finished. Results: {worker_results}")
    time.sleep(random.randint(5, 10))
    
    # Trigger the next batch
    orchestrate_reputation_guard.delay(cache_key, next_index)
    return "Next batch triggered"

