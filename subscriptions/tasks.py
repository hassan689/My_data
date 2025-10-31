from django.utils.timezone import now
from subscriptions.models import Subscription, Revenue, Expense
from warmup.models import WarmupCampaign
from users.models import EmailAccount
from datetime import datetime
from decimal import Decimal
from growth_skool.celery import app
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta
from django.utils.timezone import get_current_timezone, make_aware
from django.db.models import Sum
from django.db import transaction


@app.task(name="subscriptions.tasks.end_warmup")
def end_warmup():
    
    # set their warmup campaigns to complete where their email accounts are sender_accounts
    # Remove their accounts from those campaings as well where they are in target_accounts

    expired_or_canceled_subscriptions = Subscription.objects.filter(
        status__in=["expired", "canceled"]
    )
    for subscription in expired_or_canceled_subscriptions:
        user = subscription.user
        user_accounts = EmailAccount.objects.filter(user=user)

        # Mark campaigns as completed where user's accounts are senders
        WarmupCampaign.objects.filter(sender_account__in=user_accounts).update(status="Complete")

        # Remove user's accounts from all target lists
        for account in user_accounts:
            with transaction.atomic():
                try: # to avoid any issues
                    for campaign in WarmupCampaign.objects.filter(target_accounts=account):
                        campaign.target_accounts.remove(account)
                        campaign.save(update_fields=["target_accounts"])

                    account.is_warmup_target = False
                    account.save(update_fields=["is_warmup_target"])
                except:
                    continue


@app.task(name="subscriptions.tasks.expire_subscriptions.expire_subscriptions")
def expire_subscriptions():
    today = now().date()
    expired_subscriptions = Subscription.objects.filter(end_date__lt=today, status="active")

    for subscription in expired_subscriptions:
        subscription.status = "expired"
        subscription.save(update_fields=["status"])


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

