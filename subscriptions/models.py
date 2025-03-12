from django.db import models
from users.models import CustomUser

class Subscription(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="subscriptions")
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField(null=True, blank=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(
        max_length=50,
        choices=[("active", "Active"), ("expired", "Expired"), ("canceled", "Canceled")],
        default="active",
    )
    transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.user.update_lifetime_value()

    def __str__(self):
        return f"{self.user.username} - {self.status}"

