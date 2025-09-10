from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .models import Subscription
from users.models import EmailAccount
from warmup.models import WarmupCampaign


# --- PRE_SAVE SIGNAL to capture old status ---
@receiver(pre_save, sender=Subscription)
def capture_old_subscription_status(sender, instance, **kwargs):
    """
    Captures the existing status of a Subscription instance before it is saved (updated).
    Stores it temporarily on the instance itself.
    """
    if instance.id: # Only for existing objects (not new ones)
        try:
            original_instance = sender.objects.get(id=instance.id)
            # Attach the old status as a private attribute to the instance
            instance._old_status = original_instance.status
            instance._old_type = original_instance.type
        except sender.DoesNotExist:
            instance._old_status = None
            instance._old_type = None
    else:
        instance._old_status = None
        instance._old_type = None


# --- POST_SAVE SIGNAL to update affiliate earnings ---
@receiver(post_save, sender=Subscription)
def update_affiliate_earnings_on_subscription_save(sender, instance, created, **kwargs):
    
    # Get the old status captured by the pre_save signal, or None if not an update
    old_status = getattr(instance, '_old_status', None)
    old_type = getattr(instance, '_old_type', None)

    # If the user was type standard or premium but stepped down to basic, remove all their email addresses from warmup_targets, boolean to false
    if old_type in ["warmup", "unibox", "premium"] and instance.type == "basic":
        user = instance.user
        # email_accounts = EmailAccount.objects.filter(user=user, is_warmup_target=True)
        # for x in email_accounts:
        #     x.is_warmup_target = False
        #     x.save()
        EmailAccount.objects.filter(user=user, is_warmup_target=True).update(is_warmup_target=False)

        user_email_accounts = user.email_accounts.all()
        active_campaigns = WarmupCampaign.objects.filter(status='Active')
        for campaign in active_campaigns:
            
            campaign.target_accounts.remove(*user_email_accounts)
            if campaign.sender_account in user_email_accounts:
                campaign.status = 'Complete'
                campaign.save()


    # Only proceed if there's a referred user and a paid_amount exists
    if not instance.user.referred_by or instance.paid_amount is None:
        return

    affiliate = instance.user.referred_by
    commission_rate = affiliate.commission_percentage / Decimal('100')

    commission_to_add = Decimal('0.00')

    # Scenario 1: New subscription is created and is active
    if created and instance.status == "active":
        commission_to_add = instance.paid_amount * commission_rate

    # Scenario 2: Existing subscription's status changed from expired/canceled to active (renewal)
    elif old_status in ["expired", "canceled"] and instance.status == "active":
        commission_to_add = instance.paid_amount * commission_rate

    # Only update if a commission was calculated
    if commission_to_add > Decimal('0.00'):
        # Use transaction.atomic for atomicity: either both updates succeed or both fail
        with transaction.atomic():
            # Refresh affiliate to get the latest lifetime_earnings before updating
            # This helps prevent race conditions if multiple commissions are processed concurrently
            affiliate.refresh_from_db()
            affiliate.lifetime_earnings += commission_to_add
            affiliate.save(update_fields=['lifetime_earnings'])

