from django.db import models
from users.models import EmailAccount
from django.contrib.auth import get_user_model
from cryptography.fernet import Fernet
from django.conf import settings
import base64

User = get_user_model()

def get_cipher():
    if isinstance(settings.ENCRYPT_KEY, str):
        key = base64.urlsafe_b64decode(settings.ENCRYPT_KEY)
    else:
        key = settings.ENCRYPT_KEY
    return Fernet(key)

def encrypt_data(data: str) -> str:
    if not data:
        return ""
    cipher = get_cipher()
    return cipher.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data:
        return ""
    cipher = get_cipher()
    try:
        return cipher.decrypt(encrypted_data.encode()).decode()
    except Exception:
        return None 


class GmailToken(models.Model):
    email_account = models.OneToOneField(EmailAccount, on_delete=models.CASCADE, related_name="gmail_token", null=True, blank=True) #remove null and blank afterwards
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    expires_in = models.IntegerField()
    token_type = models.CharField(max_length=50)
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_history_id = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"{self.email_account} - {self.created_at}"
    
    def set_access_token(self, raw_token):
        if raw_token:
            self.access_token_encrypted = encrypt_data(raw_token)

    def get_access_token(self):
        return decrypt_data(self.access_token_encrypted)

    def set_refresh_token(self, raw_token):
        if raw_token:
            self.refresh_token_encrypted = encrypt_data(raw_token)

    def get_refresh_token(self):
        return decrypt_data(self.refresh_token_encrypted)


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


