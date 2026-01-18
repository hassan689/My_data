from django.utils.timezone import now, timedelta
from django.core.mail import send_mail
from django.conf import settings
from users.models import CustomUser
from growth_skool.celery import app


@app.task(
    name="users.tasks.send_expiry_email",
    bind=True,
    default_retry_delay=300, # Retry after 5 mins if SMTP fails
    max_retries=3
)
def send_expiry_email(self, user_email):
    """Sends a single trial expiration email with retry logic."""
    subject = "Your Free Trial Has Ended - Subscribe Now!"
    message = (
        "We hope you enjoyed your free trial of Dispatch Skool's automation tools. "
        "Your trial period has now ended, and we'd love for you to continue."
        "\n\nTo maintain uninterrupted access, please subscribe here: "
        "https://dispatchskool.com/#pricing"
        "\n\nBest regards,\nThe Dispatch Skool Team"
    )
    
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[user_email],
            fail_silently=False, # Set to False so we can catch and retry errors
        )
    except Exception as exc:
        # If the email server is down, this will retry the task
        print(f"❌ Error sending to {user_email}: {exc}")
        raise self.retry(exc=exc)


@app.task(name="users.tasks.check_free_trial_expiry")
def check_free_trial_expiry():
    # 1. Identify targets (Using our new index!)
    seven_days_ago = now() - timedelta(days=7)
    expired_users_qs = CustomUser.objects.filter(
        on_free_trial=True, 
        trial_started_at__lte=seven_days_ago
    )

    # 2. Grab emails BEFORE updating
    user_data = list(expired_users_qs.values_list("email", flat=True))

    if user_data:
        # 3. Bulk update in the DB (Fast SQL operation)
        expired_users_qs.update(on_free_trial=False)

        # 4. Hand off to another task for email sending
        for email in user_data:
            send_expiry_email.delay(email)

    else:
        print("No expired trials today.")
