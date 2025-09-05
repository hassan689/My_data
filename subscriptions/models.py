from django.db import models
from users.models import CustomUser
from django.utils.timezone import now
from datetime import timedelta
from decimal import Decimal


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

    type = models.CharField(
        max_length=50,
        choices=[("basic", "Basic"), ("warmup", "Warmup"), ("unibox", "Unibox"), ("premium", "Premium")],
        default="Basic", null=True, blank=True,
    )

    paid_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )

    def save(self, *args, **kwargs):
        is_new = self.id is None

        # Store the old status before saving (only if it's not a new record)
        old_status = None
        if not is_new:
            old_status = Subscription.objects.filter(id=self.id).values_list('status', flat=True).first()

        # If new, set end_date based on start_date
        if is_new:
            super().save(*args, **kwargs)  # Save once to get auto_now_add start_date
            self.end_date = self.start_date + timedelta(days=30)

        super().save(*args, **kwargs)

        # Check if status transitioned from expired to active
        if (old_status == "expired" or old_status == "canceled") and self.status == "active":
            self.renew_subscription()

    def renew_subscription(self, additional_days=30):
        """Renews the subscription by extending the end_date and increasing the renewal count."""
        self.start_date = now()
        self.end_date = self.start_date + timedelta(days=additional_days)
        self.renewal_count += 1
        self.save(update_fields=["start_date", "end_date", "renewal_count"])

    def __str__(self):
        return f"{self.user.username} - {self.status}"



class Revenue(models.Model):
    # Use the 1st day of each month to represent that month
    month = models.DateField(unique=True)
    net_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paid_to_affiliates = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return self.month.strftime("%B %Y")

    class Meta:
        verbose_name = "Monthly Revenue"
        verbose_name_plural = "Monthly Revenues"


class Expense(models.Model):
    
    name = models.CharField(max_length=250)
    description = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    rev_month = models.ForeignKey(Revenue, on_delete=models.CASCADE, related_name="expenses")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name = "Monthly Expense"
        verbose_name_plural = "Monthly Expenses"


