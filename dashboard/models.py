from django.db import models
from users.models import EmailAccount
from django.contrib.auth import get_user_model

User = get_user_model()

class GmailToken(models.Model):
    email_account = models.OneToOneField(EmailAccount, on_delete=models.CASCADE, related_name="gmail_token", null=True, blank=True) #remove null and blank afterwards
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_in = models.IntegerField()
    token_type = models.CharField(max_length=50)
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email_account} - {self.created_at}"


class CampaignRecord(models.Model):
    subject = models.CharField(max_length=255)
    body = models.TextField()
    launch_time = models.DateTimeField(auto_now_add=True)
    launched_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True) # User who initiated the campaign
    sender_account = models.ForeignKey(EmailAccount, on_delete=models.SET_NULL, null=True, blank=True) # Account from which emails were sent
    total_recipients = models.IntegerField(default=0) # To track how many emails were targeted
    sent_count = models.IntegerField(default=0) # To track how many were actually sent successfully

    def __str__(self):
        return f"Launched by {self.launched_by} via {self.sender_account} at {self.launch_time.strftime('%Y-%m-%d %H:%M')}"

    class Meta:
        verbose_name = "Campaign Launch Record"
        verbose_name_plural = "Campaign Launch Records"


