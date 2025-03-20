from django.db import models
from users.models import CustomUser
from django.utils.timezone import now
from datetime import timedelta


class Subscription(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="subscription")
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField(null=True, blank=True)
    renewal_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=50,
        choices=[("active", "Active"), ("expired", "Expired"), ("canceled", "Canceled")],
        default="active",
    )

    def save(self, *args, **kwargs):
        # If it's a new subscription, set end_date if not already set
        if not self.id and not self.end_date:
            self.end_date = self.start_date + timedelta(days=30)  # Default to 30 days

        # Fetch old status before saving
        old_subscription = None
        if self.id:
            old_subscription = Subscription.objects.get(id=self.id)

        super().save(*args, **kwargs)

        # If the status was "expired" and is now "active", renew and update user data
        if old_subscription and old_subscription.status == "expired" and self.status == "active":
            self.renew_subscription()

    def renew_subscription(self, additional_days=30):
        """Renews the subscription by extending the end_date and increasing the renewal count."""
        self.start_date = now()
        self.end_date = self.start_date + timedelta(days=additional_days)
        self.renewal_count += 1  # Increment renewal count
        self.save(update_fields=["start_date", "end_date", "renewal_count"])


    def __str__(self):
        return f"{self.user.username} - {self.status}"

