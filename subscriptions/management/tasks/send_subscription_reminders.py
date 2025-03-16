from django.utils.timezone import now
from django.core.mail import send_mail
from datetime import timedelta
from subscriptions.models import Subscription
from django.conf import settings

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
        

#  use thread pool executor