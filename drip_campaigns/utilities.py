from .models import EmailAccountAndLeads
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone
from django.conf import settings
from django.utils.encoding import force_str


# ===================================================================
# HELPER FUNCTION FOR RESCHEDULING (Updated with Cache Logic)
# ===================================================================
def reschedule_or_finalize(campaign_id, account, template, next_lead_index, delay_seconds, use_batch=False, batch_size=10):
    """
    Decides whether to reschedule the next lead/batch or finalize the account.
    Includes a 'Hybrid' check (Cache + DB Fallback) to prevent premature failures.
    """
    from .tasks import finalize_drip_step_task, send_single_email, send_batch_emails
    
    # --- CASE 1: Account finished its leads ---
    if next_lead_index >= len(account.leads_data):
        print(f"Account {account.id} finished its list for template {template.id}.")

        # 1. Mark this account as 'Ready' (Done) in the DB immediately
        if account.status != 'Stopped':
            account.status = 'Ready'
            account.save(update_fields=['status'])
        
        # 2. Check if we are the *last* account to finish
        total_key = f"drip_step_total_{template.id}"
        finished_key = f"drip_step_finished_{template.id}"
        
        trigger_finalizer = False
        
        try:
            # --- PRIMARY CHECK: CACHE (Fast) ---
            total_to_finish = cache.get(total_key)
            
            if total_to_finish is not None:
                # Cache is alive! Use it.
                current_finished = cache.incr(finished_key)
                if current_finished >= total_to_finish:
                    trigger_finalizer = True
            
            else:
                # --- SECONDARY CHECK: DATABASE (Reliable) ---
                # Cache is dead/expired. Don't panic. Check the DB.
                print(f"⚠️ Cache missing for Camp {campaign_id}. Checking DB for active workers...")
                
                # Are there any accounts still marked 'Processing'?
                others_still_running = EmailAccountAndLeads.objects.filter(
                    campaign_id=campaign_id,
                    status='Processing'
                ).exists()
                
                if not others_still_running:
                    print("DB confirms: No other accounts are Processing. We are the last.")
                    trigger_finalizer = True
                else:
                    print("DB reports: Other accounts are still running. Exiting.")

        except Exception as e:
            # If Cache crashes entirely, fallback to DB check
            print(f"Error checking cache: {e}. Fallback to DB.")
            others_still_running = EmailAccountAndLeads.objects.filter(
                campaign_id=campaign_id,
                status='Processing'
            ).exists()
            if not others_still_running:
                trigger_finalizer = True

        # 3. Trigger the Finalizer if we are the last one
        if trigger_finalizer:
            print(f"Finalizing step {template.step_number} for Campaign {campaign_id}.")
            finalize_drip_step_task.delay(campaign_id)
            
            # Cleanup (optional, harmless if keys are gone)
            cache.delete_many([total_key, finished_key])
        
        return # Done.

    # --- CASE 2: More leads to send (Reschedule) ---
    else:
        if use_batch:
            print(f"Rescheduling BATCH for {account.id} at lead {next_lead_index} in {delay_seconds}s.")
            send_batch_emails.apply_async(
                args=[campaign_id, account.id, template.id, next_lead_index, batch_size],
                countdown=delay_seconds
            )
        else:
            print(f"Rescheduling SINGLE for {account.id} for lead {next_lead_index} in {delay_seconds}s.")
            send_single_email.apply_async(
                args=[campaign_id, account.id, template.id, next_lead_index],
                countdown=delay_seconds
            )


def normalize_provider(provider_string):
    """
    Cleans the user-entered provider string to match a key
    in our IMAP_SETTINGS_MAP.
    """
    if not provider_string:
        return None
        
    provider_low = provider_string.lower()
    
    # Check for keywords
    if 'gmail' in provider_low or 'google' in provider_low:
        return 'gmail'
    if 'outlook' in provider_low or 'microsoft' in provider_low:
        return 'outlook'
    if 'yahoo' in provider_low:
        return 'yahoo'
    if 'zoho' in provider_low:
        return 'zoho'
    if 'hostinger' in provider_low:
        return 'hostinger'
    if 'namecheap' in provider_low or 'privateemail' in provider_low:
        return 'namecheap'
        
    return None # We don't recognize it


def send_campaign_failure_alert(error_message, location, campaign_id=None):
    """
    Sends a critical alert email to the developer when a campaign fails.
    """
    subject = f"CRITICAL: Drip Campaign Failure in {location}"
    
    body = (
        f"An exception occurred in {location}.\n\n"
        f"Campaign ID: {campaign_id if campaign_id else 'Unknown'}\n"
        f"Error Message:\n{error_message}\n\n"
        f"Timestamp: {timezone.now()}\n"
        f"System Status: The campaign has been marked as 'Failed' in the database."
    )
    
    recipient_list = ["abdullahatif132@gmail.com"]
    from_email = getattr(settings, 'EMAIL_HOST_USER', getattr(settings, 'DEFAULT_FROM_EMAIL', None))

    try:
        print(f"⚠️ Sending Failure Alert to {recipient_list}...")
        send_mail(
            subject=subject,
            message=force_str(body),
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=False
        )
    except Exception as e:
        print(f"CRITICAL: Failed to send failure alert email! Original error: {error_message}. Email error: {e}")

