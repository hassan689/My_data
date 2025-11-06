from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import send_warmup_step
from .models import WarmupTemplateSet, WarmupCampaign
from django.utils import timezone
from datetime import timedelta
from users.models import EmailAccount
import random


# So that they only communicate with their own accounts. Removing the conflict between users.
def refresh_targets(campaign):

    # Step 1: Get sender and its user
    sender_account = campaign.sender_account
    sender_user = sender_account.user

    # Step 2: Get all other email account instances for that user, excluding sender
    user_email_accounts = EmailAccount.objects.filter(user=sender_user, black_list=False, is_warmup_target=True).exclude(id=sender_account.id)

    # Step 3: Randomly pick up to 5
    selected_accounts = random.sample(list(user_email_accounts), min(5, user_email_accounts.count()))

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

