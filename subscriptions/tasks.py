from django.utils import timezone
from new_warmup.models import WarmupProfile
from subscriptions.models import Subscription, Revenue, Expense
# from warmup.models import WarmupCampaign
from users.models import EmailAccount
from datetime import datetime
from decimal import Decimal
from growth_skool.celery import app
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta
from django.utils.timezone import get_current_timezone, make_aware, now
from django.db.models import Sum
from django.db import transaction
from drip_campaigns.models import DripCampaign, EmailAccountAndLeads
from dashboard.models import CampaignRecord


@app.task(name="subscriptions.tasks.end_warmup")
def end_warmup():
    
    # pause all warmup and campaigns for users with expired or canceled subscriptions

    expired_or_canceled_subscriptions = Subscription.objects.filter(
        status__in=["expired", "canceled"]
    )
    for subscription in expired_or_canceled_subscriptions:
        try:
            user = subscription.user

            # Find campaigns that need to be stopped
            active_drip_campaigns = DripCampaign.objects.filter(
                launched_by=user,
                status__in=['Active', 'Processing']
            )

            if active_drip_campaigns.exists():
                campaign_ids = list(active_drip_campaigns.values_list('id', flat=True))

                with transaction.atomic():
                    
                    # Stop in-progress account chains
                    EmailAccountAndLeads.objects.filter(
                        campaign_id__in=campaign_ids,
                        status='Processing'
                    ).update(status='Stopped')

                    # Pause the Campaigns
                    active_drip_campaigns.update(status='Paused')

            active_campaigns = CampaignRecord.objects.filter(
                launched_by=user,
                status__in=['pending', 'processing']
            )
            if active_campaigns.exists():
                active_campaigns.update(status='cancelled')

            user_accounts = EmailAccount.objects.filter(user=user)
            if not user_accounts.exists():
                # No accounts for this user, skip
                continue

            # ---------------------------------------------------------
            # 2. NEW WARMUP PAUSE LOGIC
            # ---------------------------------------------------------
            # Flip the status and enabled switch for all accounts owned by this user
            WarmupProfile.objects.filter(
                email_account__user=user,
                status='Warming'
            ).update(
                status='Paused',
                warmup_enabled=False
            )
            
                    
        except Exception as e:
            print(f"Error processing subscription {subscription.id} for user {user.id}: {e}")
            continue



@app.task(name="subscriptions.tasks.expire_subscriptions.expire_subscriptions")
def expire_subscriptions():
    now = timezone.now()
    expired_subscriptions = Subscription.objects.filter(end_date__lt=now, status="active")

    message = """
Hi there,

We wanted to let you know that your subscription to Dispatch Skool Automation Tools has now expired. As a result, access to Dispatch Skool's features has been temporarily paused.

If you would like to continue using the platform without interruption, you can renew your subscription anytime by visiting our pricing page:

https://dispatchskool.com/#pricing

If you have any questions or need help renewing, just drop us a text on our WhatsApp support numbers.

Best regards,
The Dispatch Skool Team
"""
    for subscription in expired_subscriptions:
        subscription.status = "expired"
        subscription.save(update_fields=["status"])

        try:
            send_mail(
                subject="Your Subscription Has Expired",
                message=message,
                from_email=settings.EMAIL_HOST_USER,
                recipient_list=[subscription.user.email],
                fail_silently=False,
            )
        except:
            continue


@app.task(name="subscriptions.tasks.send_subscription_reminders.send_subscription_reminders")
def send_subscription_reminders():
    seven_days_from_now = now() + timedelta(days=7)
    subscriptions = Subscription.objects.filter(end_date__date=seven_days_from_now.date(), status="active")
    message = """
We wanted to remind you that your subscription to Dispatch Skool Automation Tools is set to expire in 7 days. To ensure uninterrupted access to our services, we recommend renewing your subscription at your earliest convenience.

You can do so by visting our website at https://dispatchskool.com/#pricing. If you have any questions or need assistance, feel free to reach out—we're happy to help!

Best regards,
The Dispatch Skool Team
"""

    for subscription in subscriptions:
        send_mail(
            subject="Reminder: Your Subscription Expires in 7 Days",
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[subscription.user.email],
            fail_silently=False,
        )
        


@app.task(name="subscriptions.tasks.update_current_month_revenue.update_current_month_revenue")
def update_current_month_revenue():
    
    tz = get_current_timezone()
    current_time = now().astimezone(tz)
    year, month = current_time.year, current_time.month

    month_start = make_aware(datetime(year, month, 1), timezone=tz)

    if month == 12:
        next_month_start = make_aware(datetime(year + 1, 1, 1), timezone=tz)
    else:
        next_month_start = make_aware(datetime(year, month + 1, 1), timezone=tz)

    subs = Subscription.objects.filter(
        start_date__gte=month_start,
        start_date__lt=next_month_start,
        paid_amount__isnull=False
    ).select_related("user__referred_by")

    paid_to_affiliates = Decimal("0.00")
    total_revenue = Decimal("0.00")

    for sub in subs:
        amount = sub.paid_amount
        if not amount:
            continue

        referrer = sub.user.referred_by
        if referrer and referrer.is_active:
            commission = (amount * referrer.commission_percentage / Decimal("100")).quantize(Decimal("0.01"))
            paid_to_affiliates += commission

        total_revenue += amount

    # Aggregate all expenses for the current month
    total_expenses = Expense.objects.filter(
        created_at__gte=month_start,
        created_at__lt=next_month_start,
    ).aggregate(total_amount=Sum('amount'))['total_amount'] or Decimal('0.00')

    net_revenue = total_revenue - paid_to_affiliates - total_expenses

    Revenue.objects.update_or_create(
        month=month_start.date(),
        defaults={
            "net_revenue": net_revenue,
            "paid_to_affiliates": paid_to_affiliates,
            "total_revenue": total_revenue,
        }
    )

