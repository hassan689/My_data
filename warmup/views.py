from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import activate_warmup_campaign
from .models import WarmupCampaign, WarmupTemplateSet
from users.models import EmailAccount
from datetime import timedelta
from django.utils import timezone

def start_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)
    print(f"Starting warmup for account: {sender_account.email_address}")

    if sender_account.black_list:
        messages.error(request, f"The email account {sender_account.email_address} has been black listed for warming up.")
        return redirect('dashboard:index')
    
    # If already this sender has a campaign with "completed" status, delete the previous campaign
    # existing_campaigns = WarmupCampaign.objects.filter(sender_account=sender_account)
    # for campaign in existing_campaigns:
    #     if campaign.status == 'Complete' or campaign.current_step >= 10:
    #         campaign.delete()

    try:
        template_set = WarmupTemplateSet.objects.get(name='Warmup Campaigns Templates')

        sender_account.is_warmup_target = True
        sender_account.save(update_fields=['is_warmup_target'])

        # Create or update campaign *immediately*
        campaign, created = WarmupCampaign.objects.get_or_create(
            sender_account=sender_account,
            defaults={
                "template_set": template_set,
                "status": "Active",
                "last_action_at": timezone.now(),
                "next_action_at": timezone.now() + timedelta(hours=1),
            },
        )
        if not created:
            campaign.status = "Active"
            campaign.last_action_at = timezone.now()
            campaign.next_action_at = timezone.now() + timedelta(hours=1)
            campaign.save(update_fields=["status", "last_action_at", "next_action_at"])
        
        # Trigger the Celery task to begin the warmup process
        activate_warmup_campaign.delay(sender_account.id, template_set.id, campaign.id)
        
        messages.success(request, f"Warmup campaign for {sender_account.email_address} has been started.")
        return redirect('dashboard:index')

    except WarmupTemplateSet.DoesNotExist:
        messages.error(request, "Default warmup template set not found.")
        return redirect('dashboard:index')
    


def stop_warmup_view(request, email_account_id):
    
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)
    existing_campaigns = WarmupCampaign.objects.filter(sender_account=sender_account)[:1]  # Get the most recent campaign

    if not existing_campaigns:
        messages.error(request, f"No active warmup campaign found for {sender_account.email_address}.")
        return redirect('dashboard:index')
    
    existing_campaigns[0].status = 'Complete'
    existing_campaigns[0].save(update_fields=['status'])

    messages.success(request, f"Warmup campaign for {sender_account.email_address} has been stopped.")
    return redirect('dashboard:index')

