from django.utils.timezone import now, timedelta
from django.core.mail import send_mail
from django.conf import settings
from dashboard.models import CampaignRecord
from drip_campaigns.models import DripCampaign, EmailAccountAndLeads
from new_warmup.models import WarmupProfile
from users.models import CustomUser
from growth_skool.celery import app
from django.db import transaction


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
    
    ten_days_ago = now() - timedelta(days=10)
    expired_users_qs = CustomUser.objects.filter(
        on_free_trial=True, 
        trial_started_at__lte=ten_days_ago
    )

    if expired_users_qs.exists():
        for user in expired_users_qs:
            try:
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

                # --- DRIP & OTHER CAMPAIGN CLEANUP ---
                # Pause Drip Campaigns
                active_drip = DripCampaign.objects.filter(launched_by=user, status__in=['Active', 'Processing'])
                if active_drip.exists():
                    campaign_ids = list(active_drip.values_list('id', flat=True))
                    with transaction.atomic():
                        EmailAccountAndLeads.objects.filter(campaign_id__in=campaign_ids, status='Processing').update(status='Stopped')
                        active_drip.update(status='Paused')

                # Cancel generic CampaignRecords
                CampaignRecord.objects.filter(launched_by=user, status__in=['pending', 'processing']).update(status='cancelled')

                # --- NOTIFICATION ---
                send_expiry_email.delay(user.email)

            except Exception as e:
                print(f"Error during cleanup for user {user.id}: {e}")

        # 2. Bulk update the trial status AFTER cleanup is initiated
        expired_users_qs.update(on_free_trial=False)

    else:
        print("No expired trials today.")
