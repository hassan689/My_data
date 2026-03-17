from django.db import models
from django.utils import timezone
from users.models import EmailAccount

class WarmupProfile(models.Model):
    STATUS_CHOICES = [
        ('Warming', 'Warming'),
        ('Paused', 'Paused'),
        ('Error', 'Error'),
    ]
    email_account = models.OneToOneField(EmailAccount, on_delete=models.CASCADE, related_name='warmup_profile')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Warming')
    warmup_enabled = models.BooleanField(default=True)
    
    # Volume limits mapped from Node
    daily_limit = models.IntegerField(default=40)
    current_daily = models.IntegerField(default=5)
    ramp_rate = models.IntegerField(default=3) # daily increase in the warmup volume
    reply_rate = models.IntegerField(default=35)
    
    # Metrics
    health_score = models.DecimalField(max_digits=3, decimal_places=1, default=5.0)
    warmup_age = models.IntegerField(default=0)
    
    def __str__(self):
        return f"{self.email_account.email_address}"

class DailyStat(models.Model):
    profile = models.ForeignKey(WarmupProfile, on_delete=models.CASCADE, related_name='daily_stats')
    date = models.DateField(default=timezone.now)
    
    sent = models.IntegerField(default=0)
    received = models.IntegerField(default=0)
    inbox = models.IntegerField(default=0)
    spam = models.IntegerField(default=0)
    replied = models.IntegerField(default=0)

    class Meta:
        unique_together = ('profile', 'date')

    def __str__(self):
        return f"{self.profile.email_account.email_address} - {self.date}"


class WarmupEmail(models.Model):
    message_id = models.CharField(max_length=255, unique=True)
    sender = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='sent_pool_emails')
    recipient = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='received_pool_emails')
    subject = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=50, default='sent')
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ID: {self.id}"
