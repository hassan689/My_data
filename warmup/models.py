from django.db import models
from users.models import EmailAccount
import uuid


class WarmupTemplateSet(models.Model):
    name = models.CharField(max_length=255, unique=True)
    templates = models.JSONField() # Stores all subjects/bodies organized by step

    def __str__(self):
        return self.name


class WarmupCampaign(models.Model):
    sender_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="warmup_campaigns")
    target_accounts = models.ManyToManyField(EmailAccount, related_name="target_of_warmup_campaigns")
    template_set = models.ForeignKey(WarmupTemplateSet, on_delete=models.SET_NULL, null=True, blank=True)
    current_step = models.IntegerField(default=0)
    
    CAMPAIGN_STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Active', 'Active'),
        ('Complete', 'Complete'),
        ('Failed', 'Failed'),
    ]
    status = models.CharField(max_length=20, choices=CAMPAIGN_STATUS_CHOICES, default='Pending')
    
    last_action_at = models.DateTimeField(null=True, blank=True)
    next_action_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Warmup for {self.sender_account.email_address}"


class WarmupMessage(models.Model):
    campaign = models.ForeignKey(WarmupCampaign, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="sent_warmup_messages")
    recipient = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="received_warmup_messages")

    message_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    in_reply_to_id = models.CharField(max_length=255, null=True, blank=True)
    thread_id = models.UUIDField(default=uuid.uuid4, editable=False)
    
    subject = models.CharField(max_length=255)
    body = models.TextField()
    
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message from {self.sender.email_address} to {self.recipient.email_address}"


