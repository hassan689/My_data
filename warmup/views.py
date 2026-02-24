from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import send_warmup_step
from .models import WarmupCampaign
from django.utils import timezone
from datetime import timedelta
from users.models import EmailAccount
from django.db.models import Count, Q
from django.db import transaction
import random

def refresh_targets(campaign):
    TARGET_LIMIT = 2
    MEMBERSHIP_CAP = 3
    DAILY_VELOCITY_CAP = 5
    
    sender_account = campaign.sender_account
    sender_user = sender_account.user
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        with transaction.atomic():
            # 1. Selection Stage: Get IDs only
            base_qs = EmailAccount.objects.filter(
                black_list=False, 
                is_warmup_target=True
            ).exclude(user=sender_user)

            eligible_ids = base_qs.annotate(
                active_target_count=Count(
                    'target_of_warmup_campaigns',
                    filter=Q(target_of_warmup_campaigns__status='Active')
                ),
                received_today_count=Count(
                    'received_warmup_messages',
                    filter=Q(received_warmup_messages__sent_at__gte=today_start)
                )
            ).filter(
                active_target_count__lt=MEMBERSHIP_CAP,
                received_today_count__lt=DAILY_VELOCITY_CAP
            ).order_by(
                'active_target_count', 
                'received_today_count', 
                '?'
            ).values_list('id', flat=True)[:TARGET_LIMIT * 3] # Fetch a larger pool to handle skip_locked

            # 2. Locking Stage: Lock the actual rows using the IDs
            # We use the list of IDs to perform a clean query without GROUP BY
            selected_accounts = list(
                EmailAccount.objects.filter(id__in=eligible_ids)
                .select_for_update(skip_locked=True)[:TARGET_LIMIT]
            )

            # 3. Assignment
            if len(selected_accounts) >= TARGET_LIMIT:
                return selected_accounts
            else:
                # Starvation fallback
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 2))
                campaign.save(update_fields=['next_action_at'])
                return []

    except Exception as e:
        print(f"Error in refresh_targets for campaign {campaign.id}: {e}")
        return []


def start_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)

    if sender_account.black_list:
        messages.error(request, f"The email account {sender_account.email_address} has been black listed for warming up.")
        return redirect('dashboard:index')

    try:
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
    
    except Exception as e:
        print("Error starting warmup:", str(e))


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

