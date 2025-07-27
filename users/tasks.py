from django.utils.timezone import now, timedelta
from django.core.mail import send_mail
from django.conf import settings
from users.models import CustomUser
from concurrent.futures import ThreadPoolExecutor
from growth_skool.celery import app


def send_expiry_email(user_email):
    """Send an email notification when the free trial expires."""
    message = """
We hope you enjoyed your free trial of Dispatch Skool's automation tools. Your trial period has now ended, and we'd love for you to continue experiencing the benefits of our services.

To maintain uninterrupted access, please subscribe at your earliest convenience. You can do so by visting our website at https://dispatchskool.com/#pricing

If you have any questions or need assistance, feel free to reach out. We're happy to help!

Best regards,
The Dispatch Skool Team
"""
    try:
        send_mail(
            subject="Your Free Trial Has Ended - Subscribe Now!",
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[user_email],
            fail_silently=True,
        )
        print(f"📧 Email sent to {user_email}")
    except Exception as e:
        print(f"❌ Failed to send email to {user_email}: {e}")


@app.task(name="users.tasks.check_free_trial_expiry")
def check_free_trial_expiry():
    """Check for expired free trials and notify users asynchronously."""
    expired_users = CustomUser.objects.filter(
        on_free_trial=True, 
        date_joined__lte=now() - timedelta(days=7)
    )

    # Set on_free_trial = False for all expired users
    expired_users.update(on_free_trial=False)

    # Collect emails for users who need notifications
    user_emails = list(expired_users.values_list("email", flat=True))

    if user_emails:
        with ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(send_expiry_email, user_emails)
    else:
        print("⚠️ No expired free trial users found.")
