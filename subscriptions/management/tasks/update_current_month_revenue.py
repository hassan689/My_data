from django.utils.timezone import now
from datetime import datetime
from decimal import Decimal
from subscriptions.models import Subscription, Revenue


def update_current_month_revenue():
    current_time = now()
    year, month = current_time.year, current_time.month

    # First day of this month (UTC aware)
    month_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # First day of next month
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1, tzinfo=current_time.tzinfo)
    else:
        next_month_start = datetime(year, month + 1, 1, tzinfo=current_time.tzinfo)

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



