from django.utils.timezone import now
from subscriptions.models import Subscription


def expire_subscriptions():
    today = now().date()
    expired_subscriptions = Subscription.objects.filter(end_date__lt=today, status="active")

    for subscription in expired_subscriptions:
        subscription.status = "expired"
        subscription.save(update_fields=["status"])


#  notify user by email
