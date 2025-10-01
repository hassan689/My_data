from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import send_warmup_step
from .models import WarmupTemplateSet, WarmupCampaign
from django.utils import timezone
from datetime import timedelta
from users.models import EmailAccount


def refresh_targets(campaign):
    """Return a list of new target accounts for a campaign."""
    return list(
        EmailAccount.objects.filter(
            is_warmup_target=True
        ).exclude(
            id=campaign.sender_account.id
        ).exclude(
            black_list=True
        ).order_by('?')[:5]
    )


def start_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)

    if sender_account.black_list:
        messages.error(request, f"The email account {sender_account.email_address} has been black listed for warming up.")
        return redirect('dashboard:index')

    try:
        template_set = WarmupTemplateSet.objects.get(name='Warmup Campaigns Templates')

        sender_account.is_warmup_target = True
        sender_account.save(update_fields=['is_warmup_target'])
        
        campaign = WarmupCampaign.objects.filter(
            sender_account=sender_account
        ).order_by('-created_at').first()

        if campaign:
            # Found a campaign, reactivate it
            campaign.status = 'Active'
            campaign.current_step = 0
            campaign.save(update_fields=['status', 'current_step'])
        else:
            # Create a new campaign
            campaign = WarmupCampaign.objects.create(
                sender_account=sender_account,
                template_set=template_set,
                status='Active',
                current_step=0,
                next_action_at=timezone.now() + timedelta(minutes=5)  # First step after 5 minutes
            )
            targets = refresh_targets(campaign)
            if targets:
                campaign.target_accounts.set(targets)

        send_warmup_step.delay(campaign.id, campaign.current_step)

        messages.success(request, f"Warmup campaign for {sender_account.email_address} has been started.")
        return redirect('dashboard:index')

    except WarmupTemplateSet.DoesNotExist:
        messages.error(request, "Default warmup template set not found.")
        return redirect('dashboard:index')


def stop_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)

    # 1. Mark all active campaign as Complete
    sender_account.warmup_campaigns.filter(status='Active').update(status = 'Complete')

    # 2. Remove this account from target_accounts of all campaigns
    target_campaigns = WarmupCampaign.objects.filter(target_accounts=sender_account)
    for campaign in target_campaigns:
        campaign.target_accounts.remove(sender_account)
        campaign.save()

    # 3. Update sender account flags
    sender_account.is_warmup_target = False
    sender_account.save(update_fields=['is_warmup_target'])

    messages.success(request, f"Warmup stopped for {sender_account.email_address}.")
    return redirect('dashboard:index')

