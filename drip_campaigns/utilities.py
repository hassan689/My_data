from .models import DripCampaign
from django.core.cache import cache


# ===================================================================
# HELPER FUNCTION FOR RESCHEDULING (Updated with Cache Logic)
# ===================================================================
def reschedule_or_finalize(campaign_id, account, template, next_lead_index, delay_seconds):
    """
    Helper function to decide whether to reschedule the next lead 
    or finalize the account's contribution to the step.
    """

    from .tasks import finalize_drip_step_task, send_single_email
    
    # Check if this was the last lead
    if next_lead_index >= account.recipient_count:
        print(f"Account {account.id} finished its list for template {template.id}.")
        
        # Define cache keys
        total_key = f"drip_step_total_{template.id}"
        finished_key = f"drip_step_finished_{template.id}"
        
        is_last_account_to_finish = False
        try:
            # Atomically increment the "finished" counter
            current_finished_count = cache.incr(finished_key)
            total_to_finish = cache.get(total_key)
            
            if total_to_finish is None:
                # This should not happen, but if it does, fail the campaign
                print(f"CRITICAL: Cache key {total_key} expired! Failing campaign.")
                DripCampaign.objects.filter(id=campaign_id).update(status='Failed')
                return

            if current_finished_count == total_to_finish:
                is_last_account_to_finish = True
                
        except Exception as e:
            print(f"CRITICAL: Failed to increment cache for template {template.id}: {e}")
            DripCampaign.objects.filter(id=campaign_id).update(status='Failed')
            return

        # If this was the last account for the whole step, call the Finisher
        if is_last_account_to_finish:
            print(f"This was the last account (count {current_finished_count}/{total_to_finish}) for step {template.step_number}. Calling finalizer.")
            finalize_drip_step_task.delay(campaign_id)
            
            # Clean up cache keys
            cache.delete_many([total_key, finished_key])
        
        return # This account's chain ends

    else:
        # More leads to send. Reschedule for the next lead.
        print(f"Rescheduling {account.id} for lead {next_lead_index} in {delay_seconds} seconds.")
        send_single_email.apply_async(
            args=[campaign_id, account.id, template.id, next_lead_index],
            countdown=delay_seconds
        )
