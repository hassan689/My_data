import random
import requests
import uuid
import time
from django.utils import timezone
from django.db.models import F, Sum
from django.core.mail import EmailMultiAlternatives
from email.utils import formataddr, make_msgid
from datetime import timedelta
from .models import WarmupProfile, DailyStat, WarmupEmail
from users.models import EmailAccount
from .utilities import *
from django.conf import settings
from django.db.models import Q
from django.db import transaction
from django.core.cache import cache
from celery import chord, shared_task, group
from growth_skool.celery import app


@app.task(name="warmup.tasks.orchestrate_daily_warmup")
def orchestrate_daily_warmup():
    """
    Runs Daily at 9 AM. Sets up daily stats and triggers the first send for each account.
    """
    profiles = WarmupProfile.objects.filter(status='Warming', warmup_enabled=True)
    today = timezone.now().date()

    for profile in profiles:
        # 1. Create today's stat tracker
        stat, created = DailyStat.objects.get_or_create(profile=profile, date=today)
        
        # 2. Calculate today's volume (current_daily +/- 20% variance)
        base_volume = min(profile.current_daily, profile.daily_limit)
        variance = int(base_volume * 0.2)
        today_target = base_volume + random.randint(-variance, variance)
        
        # Prevent zero or negative sends
        if today_target <= 0: continue
        
        # 3. If they haven't finished today's quota, trigger the first worker
        if stat.sent < today_target:
            # Spread the very first sends out over the first hour to avoid spikes at 9 AM. Random delay between 10 seconds and 1 hour.
            initial_delay = random.randint(10, 3600) 
            send_single_warmup_email.apply_async(
                args=[profile.id, today_target], 
                countdown=initial_delay
            )


@shared_task(name="warmup.tasks.send_single_warmup_email", max_retries=1)
def send_single_warmup_email(profile_id, target_volume):
    """
    The recursive worker. Checks DB quota, sends 1 email, updates DB, schedules itself.
    """
    try:
        profile = WarmupProfile.objects.select_related('email_account', 'email_account__user').get(id=profile_id)

        # 0. Safety Check: If profile is paused or disabled, abort.
        if profile.status != 'Warming' or not profile.warmup_enabled:
            return "Warmup paused or disabled. Aborting task."

        sender_account = profile.email_account
        today = timezone.now().date()
        
        # 1. Check DB: Are we done for the day?
        stat, _ = DailyStat.objects.get_or_create(profile=profile, date=today)
        if stat.sent >= target_volume:
            return "Daily quota met."

        # 2. Find Priority Target
        recipient_account = get_water_level_target(sender_account)
        if not recipient_account:
            return "No valid targets found."

        # 3. Prepare Email
        subject, body = generate_fresh_spintax(
            recipient_account.user.first_name, 
            getattr(sender_account.user, "company_name", "ABC Transports"),
            getattr(recipient_account.user, "company_name", "XYZ Logistics")
        )
        new_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
        from_email = formataddr((sender_account.display_name or '', sender_account.email_address))
        
        # 4. Connect & Send
        connection = get_email_connection(sender_account, sender_account.get_password())
        if not connection:
            # If SMTP fails totally, fail softly so finally block reschedules later
            raise Exception("SMTP Connection Failed")

        try:
            main_msg = EmailMultiAlternatives(
                subject=subject, body=body, from_email=from_email,
                to=[recipient_account.email_address], connection=connection
            )
            main_msg.extra_headers = {'Message-ID': new_msg_id, 'X-Warmup-ID': new_msg_id}
            main_msg.encoding = 'utf-8'
            main_msg.send()

            msg = f"--- [SENDING] {sender_account.email_address} -> {recipient_account.email_address})"
            print(msg)

            # 5. Log it and Update Atomic Stats
            WarmupEmail.objects.create(
                message_id=new_msg_id, sender=sender_account, recipient=recipient_account,
                subject=subject, body=body
            )
            DailyStat.objects.filter(profile=profile, date=today).update(sent=F('sent') + 1)
            
            # Recipient received stat update
            recipient_profile = getattr(recipient_account, 'warmup_profile', None)
            if recipient_profile:
                DailyStat.objects.filter(profile=recipient_profile, date=today).update(received=F('received') + 1)

        finally:
            connection.close()

    except Exception as e:
        print(f"[Warmup Worker Error] Profile {profile_id}: {e}")

    finally:
        # 6. RECURSION: Calculate remaining and schedule next email
        try:
            current_stat = DailyStat.objects.get(profile_id=profile_id, date=timezone.now().date())
            if current_stat.sent < target_volume:
                # Calculate interval: Spread remaining emails over remaining hours in the 9AM-6PM window
                next_delay = random.randint(900, 1500) 
                
                send_single_warmup_email.apply_async(
                    args=[profile_id, target_volume], 
                    countdown=next_delay
                )
        except Exception:
            pass


@app.task(name="warmup.tasks.midnight_maintenance")
def midnight_maintenance():
    """
    Runs daily at midnight. 
    Increments age, ramps up daily send limits, and recalculates the 7-day health score.
    """
    profiles = WarmupProfile.objects.filter(status='Warming', warmup_enabled=True)
    seven_days_ago = timezone.now().date() - timedelta(days=7)
    
    profiles_to_update = []

    for profile in profiles:
        # 1. Increment Warmup Age
        profile.warmup_age += 1

        # 2. Ramp Up Daily Volume
        if profile.current_daily < profile.daily_limit:
            profile.current_daily = min(
                profile.current_daily + profile.ramp_rate, 
                profile.daily_limit
            )

        # 3. Recalculate Health Score (7-Day Rolling Window)
        stats = DailyStat.objects.filter(
            profile=profile, 
            date__gte=seven_days_ago
        ).aggregate(
            total_inbox=Sum('inbox'),
            total_spam=Sum('spam')
        )

        total_inbox = stats['total_inbox'] or 0
        total_spam = stats['total_spam'] or 0
        total_delivered = total_inbox + total_spam

        # Health Scoring Logic
        score = 5.0
        if total_delivered > 0:
            inbox_rate = total_inbox / total_delivered
            spam_rate = total_spam / total_delivered

            # Inbox rewards
            if inbox_rate > 0.90: score += 3.0
            elif inbox_rate > 0.80: score += 2.0
            elif inbox_rate > 0.70: score += 1.0

            # Spam penalties/rewards
            if spam_rate < 0.05: score += 2.0
            elif spam_rate < 0.10: score += 1.0
            elif spam_rate > 0.20: score -= 2.0

        # Cap score between 1.0 and 10.0
        profile.health_score = max(1.0, min(10.0, score))
        
        profiles_to_update.append(profile)

    # 4. Execute a single bulk database hit
    if profiles_to_update:
        WarmupProfile.objects.bulk_update(
            profiles_to_update, 
            ['warmup_age', 'current_daily', 'health_score']
        )


########### IMAP Workers Pipeline ###########

@shared_task(name="warmup.tasks.orchestrate_imap_cycle")
def orchestrate_imap_cycle(cache_key=None, current_index=0):
    BATCH_SIZE = 15

    if cache_key is None:
        # Start of a new cycle: Fetch all active profiles
        profile_ids = list(WarmupProfile.objects.filter(
            status='Warming', warmup_enabled=True
        ).values_list('id', flat=True))
        
        if not profile_ids: return "No accounts to process"
        
        cache_key = f"imap_cycle_{uuid.uuid4().hex}"
        cache.set(cache_key, profile_ids, timeout=36000)
    else:
        profile_ids = cache.get(cache_key)
    
    if not profile_ids: return "Cache expired or empty"

    # Update Watchdog
    cache.set('last_imap_cycle_activity', timezone.now().timestamp(), timeout=14400)

    batch_ids = profile_ids[current_index : current_index + BATCH_SIZE]
    next_index = current_index + BATCH_SIZE
    
    if not batch_ids:
        # Cycle Complete. Clean up and schedule the next cycle in 30 minutes.
        cache.delete(cache_key)
        orchestrate_imap_cycle.apply_async(countdown=1800)
        return "Cycle Complete. Next cycle scheduled."

    # Launch Chord
    header = [process_single_imap_account.s(p_id) for p_id in batch_ids]
    callback = imap_batch_callback.s(cache_key, next_index)
    
    return chord(header)(callback)


@shared_task(name="warmup.tasks.imap_batch_callback")
def imap_batch_callback(worker_results, cache_key, next_index):
    """Fired after a batch of 15 workers finish."""
    # Update Watchdog
    cache.set('last_imap_cycle_activity', timezone.now().timestamp(), timeout=14400)
    
    # Optional slight pause to be gentle on your server CPU
    time.sleep(2)
    
    orchestrate_imap_cycle.delay(cache_key, next_index)
    return f"Batch processed. Triggering index {next_index}"


@app.task(name="warmup.tasks.imap_watchdog")
def imap_watchdog():
    """Beat task: Runs every 2 hours. Restarts cycle if chord died."""
    last_activity = cache.get('last_imap_cycle_activity')
    now = timezone.now().timestamp()
    
    # If no activity for 90 minutes, or key is missing, restart
    if not last_activity or (now - last_activity > 5400):
        print("[WATCHDOG] IMAP cycle dead. Restarting.")
        orchestrate_imap_cycle.delay()


@shared_task(name="warmup.tasks.process_single_imap_account", soft_time_limit=60, time_limit=70)
def process_single_imap_account(profile_id):
    try:
        profile = WarmupProfile.objects.select_related('email_account').get(id=profile_id)
        account = profile.email_account
        
        imap_conn = get_warmup_imap_connection(account)
        if not imap_conn:
            print(f"IMAP connection failed for {account.email_address}")
            return f"Failed: {account.email_address} (Conn)"
        
        today = timezone.now().date()
        
        try:
            # 1. Rescue from Spam
            rescued = rescue_from_spam(imap_conn, account.email_address)
            if rescued > 0:
                DailyStat.objects.filter(profile=profile, date=today).update(
                    spam=F('spam') + rescued
                )

            # 2. Check Inbox & Roll for Replies
            inbox_emails = process_single_inbox(imap_conn)
            inbox_count = len(inbox_emails)
            if inbox_count > 0:
                DailyStat.objects.filter(profile=profile, date=today).update(inbox=F('inbox') + inbox_count)

            replies_triggered = 0
            
            for email_data in inbox_emails:
                # The Dice Roll
                if random.randint(1, 100) <= profile.reply_rate:
                    # Fire the decoupled SMTP task!
                    send_reply_to_warmup_email.delay(
                        profile.id, 
                        email_data['from_email'], 
                        email_data['subject'], 
                        email_data['message_id'],
                        email_data['body']
                    )
                    replies_triggered += 1
            
            return f"Success: {account.email_address} (Rescued {rescued}, Replying {replies_triggered})"
            
        finally:
            try: imap_conn.logout() 
            except: pass
            
    except Exception as e:
        print(f"IMAP processing error for profile {profile_id}: {e}")
        return f"Error: {str(e)}"


@shared_task(name="warmup.tasks.send_reply_to_warmup_email", max_retries=1)
def send_reply_to_warmup_email(sender_profile_id, target_email, original_subject, original_msg_id, original_body):
    """
    Dedicated short-lived worker purely for sending a reply.
    """
    try:
        profile = WarmupProfile.objects.select_related('email_account', 'email_account__user').get(id=sender_profile_id)
        sender_account = profile.email_account
        
        # 1. Stateless Thread Stitching
        reply_subject = original_subject if original_subject.lower().startswith('re:') else f"Re: {original_subject}"
        
        # Generate a generic conversational body
        fresh_body = generate_spintax_body(sender_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports"))
        reply_body = f"{fresh_body}\n\n> {original_body.replace(chr(10), chr(10)+'> ')}"
        
        new_msg_id = make_msgid(idstring=uuid.uuid4().hex, domain='dispatchskool.com')
        from_email = formataddr((sender_account.display_name or '', sender_account.email_address))

        # 2. Connect & Send
        connection = get_email_connection(sender_account, sender_account.get_password())
        if not connection: return
        
        try:
            reply_msg = EmailMultiAlternatives(
                subject=reply_subject, body=reply_body, from_email=from_email,
                to=[target_email], connection=connection
            )
            # Injecting standard threading headers + our tracking header
            reply_msg.extra_headers = {
                'Message-ID': new_msg_id, 
                'In-Reply-To': original_msg_id,
                'References': original_msg_id,
                'X-Warmup-ID': new_msg_id
            }
            reply_msg.encoding = 'utf-8'
            reply_msg.send()
            
            # 3. Log the reply
            today = timezone.now().date()
            DailyStat.objects.filter(profile=profile, date=today).update(replied=F('replied') + 1)
            
        finally:
            connection.close()
            
    except Exception as e:
        print(f"Reply worker failed: {e}")



############ Accounts cleanup ############

# A robust Celery task using your Mails.so API to proactively blacklist bad accounts.

AUDIT_API_SUBMIT_URL = 'https://api.mails.so/v1/batch'
AUDIT_CHUNK_SIZE = 500
AUDIT_POLL_INTERVAL = 60
AUDIT_HARD_DEADLINE = 300

@app.task(name="warmup.tasks.audit_warmup_targets")
def audit_warmup_targets():
    # 1. Identify candidates
    candidates = EmailAccount.objects.filter(
        Q(user__subscription__status='active') | Q(user__on_free_trial=True),
        black_list=False,
        warmup_profile__status='Warming'
    ).distinct().values_list('email_address', flat=True)

    candidate_list = list(candidates)
    if not candidate_list:
        return "No warmup candidates found."

    # 2. Define the Chord: [Group of Workers] -> Callback
    # Using the structure from your working app
    job = chord(
        group(
            verify_warmup_chunk.s(
                candidate_list[i:i + AUDIT_CHUNK_SIZE], 
                time.time()
            ) 
            for i in range(0, len(candidate_list), AUDIT_CHUNK_SIZE)
        ),
        finalize_warmup_audit.s()
    )
    job.delay()
    return f"Dispatched {len(candidate_list)} accounts in chunks."


@shared_task(bind=True, name="warmup.tasks.verify_warmup_chunk", max_retries=10)
def verify_warmup_chunk(self, email_list, start_time, job_id=None, processed_map=None):
    elapsed = time.time() - start_time
    processed_map = processed_map or {}
    
    API_KEY = getattr(settings, 'MAILS_SO_API_KEY', '')
    headers = {'Content-Type': 'application/json', 'x-mails-api-key': API_KEY}

    # 1. HARD STOP - Return what we have so far
    if elapsed >= AUDIT_HARD_DEADLINE:
        return processed_map

    # 2. SUBMISSION PHASE
    if not job_id:
        pending = [e.lower() for e in email_list if e.lower() not in processed_map]
        if not pending: return processed_map

        try:
            resp = requests.post(AUDIT_API_SUBMIT_URL, headers=headers, json={'emails': pending}, timeout=30)
            if resp.status_code in (200, 201, 202):
                job_id = resp.json().get('id')
            else:
                raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': None, 'processed_map': processed_map})
        except Exception:
            raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': None, 'processed_map': processed_map})

    # 3. POLLING PHASE
    try:
        poll_resp = requests.get(f"{AUDIT_API_SUBMIT_URL}/{job_id}", headers=headers, timeout=30)
        if poll_resp.status_code == 200:
            data = poll_resp.json()
            for r in data.get('emails', []):
                processed_map[str(r.get('email', '')).lower()] = r

            if data.get('status') == 'completed':
                return processed_map

    except Exception:
        pass 

    raise self.retry(countdown=AUDIT_POLL_INTERVAL, kwargs={'job_id': job_id, 'processed_map': processed_map})


@shared_task(name="warmup.tasks.finalize_warmup_audit")
def finalize_warmup_audit(results_list):
    """
    results_list is a list of 'processed_map' dicts from all chunks.
    """
    # 1. Merge all chunk results into one master map
    master_map = {}
    for chunk_map in results_list:
        master_map.update(chunk_map)

    if not master_map:
        return "No results to process."

    emails = list(master_map.keys())
    
    # 2. Fetch all relevant accounts
    accounts = EmailAccount.objects.select_related('warmup_profile').filter(
        email_address__iregex=r'^(' + '|'.join(map(re.escape, emails)) + r')$'
    )

    accounts_to_update = []
    profiles_to_update = []

    for account in accounts:
        data = master_map.get(account.email_address.lower(), {})
        status = str(data.get('status', data.get('result', ''))).lower()
        
        # Logic for blacklisting undeliverable accounts
        if status == 'undeliverable' and not account.black_list:
            account.black_list = True
            account.is_warmup_target = False
            accounts_to_update.append(account)

            profile = getattr(account, 'warmup_profile', None)
            if profile:
                profile.status = 'Error'
                profile.warmup_enabled = False
                profiles_to_update.append(profile)

    # 3. Atomic Bulk Update
    if accounts_to_update or profiles_to_update:
        try:
            with transaction.atomic():
                if accounts_to_update:
                    EmailAccount.objects.bulk_update(accounts_to_update, ['black_list', 'is_warmup_target'])
                if profiles_to_update:
                    WarmupProfile.objects.bulk_update(profiles_to_update, ['status', 'warmup_enabled'])
            return f"Audit Success: Blacklisted {len(accounts_to_update)} accounts."
        except Exception as e:
            return f"Database error during finalize: {e}"
    
    return "Audit complete: No new accounts required blacklisting."

