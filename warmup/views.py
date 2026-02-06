from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import send_warmup_step
from .models import WarmupTemplateSet, WarmupCampaign
from django.utils import timezone
from datetime import timedelta
from users.models import EmailAccount
import random


def refresh_targets(campaign):
    """
    Refreshes the target list for a campaign.
    Priority 1: 'Idle' accounts (Not currently a target in any ACTIVE campaign).
    Priority 2: 'Busy' accounts (Already targeted, used as fill-in).
    """
    target_count = 5
    sender_account = campaign.sender_account
    sender_user = sender_account.user

    # 1. Base Pool: Eligible accounts, excluding the sender's own user
    # We exclude the sender_user entirely to prevent self-warming loops within one user account
    base_qs = EmailAccount.objects.filter(
        black_list=False, 
        is_warmup_target=True
    ).exclude(user=sender_user)

    # 2. Priority Pool: Find accounts that are NOT in any 'Active' campaign right now
    # We use the related_name 'target_of_warmup_campaigns' to check status
    idle_accounts_qs = base_qs.exclude(target_of_warmup_campaigns__status='Active')
    idle_accounts = list(idle_accounts_qs)

    selected_accounts = []

    # 3. Selection Logic
    if len(idle_accounts) >= target_count:
        # Ideal: We have enough idle accounts to fill the slots
        selected_accounts = random.sample(idle_accounts, target_count)
    else:
        # Scarcity: Take all idle accounts, then fill the remainder with busy ones
        selected_accounts = idle_accounts[:] # Take them all
        needed = target_count - len(selected_accounts)
        
        if needed > 0:
            
            # Get IDs of accounts we already selected to exclude them
            selected_ids = [acc.id for acc in selected_accounts]
            
            busy_accounts_qs = base_qs.filter(
                target_of_warmup_campaigns__status='Active'
            ).exclude(id__in=selected_ids).distinct()
            
            busy_accounts = list(busy_accounts_qs)

            # Fill the rest
            if len(busy_accounts) >= needed:
                selected_accounts.extend(random.sample(busy_accounts, needed))
            else:
                # If we still don't have enough, just take what exists
                selected_accounts.extend(busy_accounts)
    
    return selected_accounts


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
            print(f"Reactivating campaign {campaign.id}")
            campaign.status = 'Active'
            campaign.current_step = 0
            campaign.save(update_fields=['status', 'current_step'])
        else:
            # Create a new campaign
            print("Creating new campaign")
            campaign = WarmupCampaign.objects.create(
                sender_account=sender_account,
                template_set=template_set,
                status='Active',
                current_step=0,
                next_action_at=timezone.now() + timedelta(minutes=5)  # First step after 5 minutes
            )

        # Refresh targets *after* the if/else block, so it
        # runs every time the warmup is started.
        
        print(f"Refreshing targets for campaign {campaign.id}...")
        targets = refresh_targets(campaign)
        
        if targets:
            print(f"Assigning {len(targets)} targets.")
            campaign.target_accounts.set(targets)

        # This call was already in the right place
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
    target_campaigns_manager = sender_account.target_of_warmup_campaigns
    campaign_count = target_campaigns_manager.count()

    print(f"Removing {sender_account.email_address} from {campaign_count} campaigns where it is a target...")

    sender_account.target_of_warmup_campaigns.clear()

    # target_campaigns = WarmupCampaign.objects.filter(target_accounts=sender_account)
    # print(f"Removing {sender_account.email_address} from {target_campaigns.count()} campaigns' targets.")
    # for campaign in target_campaigns:
    #     campaign.target_accounts.remove(sender_account)
    #     campaign.save()
    #     print(campaign.target_accounts.all())

    # 3. Update sender account flags
    sender_account.is_warmup_target = False
    sender_account.save(update_fields=['is_warmup_target'])

    messages.success(request, f"Warmup stopped for {sender_account.email_address}.")
    return redirect('dashboard:index')

