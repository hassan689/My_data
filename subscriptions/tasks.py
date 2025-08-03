from django.utils.timezone import now
from subscriptions.models import Subscription, Revenue
from datetime import datetime
from decimal import Decimal
from growth_skool.celery import app
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta
from django.utils.timezone import get_current_timezone, make_aware


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

    # Fetch subscriptions created this month with valid payment
    subs = Subscription.objects.filter(
        start_date__gte=month_start,
        start_date__lt=next_month_start,
        paid_amount__isnull=False
    ).select_related("user__referred_by")

    net_revenue = Decimal("0.00")
    paid_to_affiliates = Decimal("0.00")

    for sub in subs:
        amount = sub.paid_amount
        if not amount:
            continue

        referrer = sub.user.referred_by

        if referrer and referrer.is_active:
            commission = (amount * referrer.commission_percentage / Decimal("100")).quantize(Decimal("0.01"))
            paid_to_affiliates += commission
            net_revenue += (amount - commission)
        else:
            net_revenue += amount

    # Create or update revenue for current month
    Revenue.objects.update_or_create(
        month=month_start.date(),
        defaults={
            "net_revenue": net_revenue,
            "paid_to_affiliates": paid_to_affiliates,
        }
    )



