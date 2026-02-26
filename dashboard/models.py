from django.db import models
from users.models import EmailAccount
from django.contrib.auth import get_user_model
from cryptography.fernet import Fernet
from django.conf import settings
import base64
from django.utils import timezone

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


class CampaignTemplate(models.Model):
    """
    New model to support A/B testing and multiple templates per campaign.
    """
    subject = models.CharField(max_length=255)
    body = models.TextField()
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="campaign_templates")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    track_template = models.BooleanField(default=False) # Changed from track_campaign
    open_rate = models.IntegerField(default=0)

    include_unsubscribe = models.BooleanField(
        default=False, 
        verbose_name="Include Unsubscribe Link"
    )

    def __str__(self):
        return f"Template: {self.subject[:30]}..."

class CampaignRecord(models.Model):
    subject = models.CharField(max_length=255, null=True, blank=True)
    body = models.TextField(null=True, blank=True)

    # --- New Architecture (M2M) ---
    templates = models.ManyToManyField(CampaignTemplate, blank=True, related_name="campaigns")

    launch_time = models.DateTimeField(auto_now_add=True)
    launched_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    sender_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, null=True, blank=True, related_name="campaigns")
    total_recipients = models.IntegerField(default=0)
    sent_count = models.IntegerField(default=0)

    leads_data = models.JSONField(default=list, null=True, blank=True) # Stores the list of leads as JSON

    sent_emails = models.JSONField(default=list, blank=True, null=True) # Stores the email addresses where email went to prevent dbl sending

    min_delay = models.IntegerField(default=0, null=True, blank=True)
    max_delay = models.IntegerField(default=0, null=True, blank=True)
    scheduled_launch_time = models.DateTimeField(null=True, blank=True) # When the campaign is set to launch

    track_campaign = models.BooleanField(default=False)
    open_rate = models.IntegerField(default=0)

    # Gate lock to prevent multiple celery workers picking up the campaign
    is_campaign_dispatched = models.BooleanField(default=False)

    CAMPAIGN_STATUS_CHOICES = [
        ('pending', 'Pending'), # schduled for later
        ('launched', 'Launched'), # campaign finnished
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'), # stopped midway
        ('processing', 'Processing'), # emails are going out
    ]
    status = models.CharField(max_length=20, choices=CAMPAIGN_STATUS_CHOICES, null=True, blank=True)

    LEAD_SOURCE_CHOICES = [
        ('Excel', 'Excel'),
        ('DB', 'DB'),
    ]
    lead_source = models.CharField(max_length=10, choices=LEAD_SOURCE_CHOICES, null=True, blank=True)

    def __str__(self):
        return f"Launched by {self.launched_by} via {self.sender_account} at {self.launch_time.strftime('%Y-%m-%d %H:%M')}"
    
    def get_assigned_template(self, lead_index=0):
        """
        Returns a Template object (or a shim) based on the Round Robin logic.
        Handles both V2 (M2M) and Legacy (Direct fields) records.
        """
        # 1. Try to fetch from M2M (V2 Logic)
        # Note: In a loop, you might want to prefetch this to avoid N+1 queries
        active_templates = list(self.templates.all())
        
        if active_templates:
            # Round Robin Math: Index % Count
            return active_templates[lead_index % len(active_templates)]
        
        # 2. Fallback to Legacy Logic
        # Return a simple object that mimics a CampaignTemplate so the worker code doesn't break
        return LegacyTemplateShim(self.subject, self.body)

    class Meta:
        verbose_name = "Campaign Launch Record"
        verbose_name_plural = "Campaign Launch Records"


class LegacyTemplateShim:
    """
    A temporary helper class to make old data look like a CampaignTemplate object.
    This allows the worker to treat everything as an object with .subject, .body, and .id
    """
    def __init__(self, subject, body):
        self.subject = subject
        self.body = body
        self.id = None  # Legacy records have no Template ID


class EmailOpen(models.Model):
    campaign = models.ForeignKey(CampaignRecord, on_delete=models.SET_NULL, null=True, blank=True) # Need to change this to set null so even if campaign is deleted, the open records remain
    recipient_email = models.CharField(max_length=255)
    unique_identifier = models.UUIDField(unique=True)
    is_opened = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True, blank=True, null=True)

    lead_snapshot = models.JSONField(default=dict, blank=True, null=True)

    # We set NULL on delete so we don't lose the "Open" event if the template is deleted later.
    template = models.ForeignKey(CampaignTemplate, on_delete=models.SET_NULL, null=True, blank=True)

    mc_number = models.CharField(max_length=50, verbose_name="MC Number", blank=True, null=True)
    legal_name = models.CharField(max_length=255, blank=True, null=True)

    launched_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True) # so each user can get their own open tracking data

    def __str__(self):
        return f"Open for {self.recipient_email} in Campaign {self.campaign.id}"



class VerificationUsage(models.Model):
    """
    Tracks the user's daily verification consumption.
    This is the 'wallet' that resets every 24 hours after the first use.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='verification_usage')
    used_count = models.PositiveIntegerField(default=0)
    next_reset_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Verification Usage"
        verbose_name_plural = "Verification Usages"
    
    def check_and_reset(self):
        """
        Checks if the 24-hour window has passed. 
        If yes, resets the counter to 0 and clears the timer.
        """
        if self.next_reset_at and timezone.now() >= self.next_reset_at:
            self.used_count = 0
            self.next_reset_at = None  # Timer clears, waiting for next upload to start new cycle
            self.save()

    def get_limit(self):
        """
        Determines limit based on Subscription type.
        """

        # Superusers have unlimited usage
        if self.user.is_superuser:
            return float('inf')
        
        # Check Free Trial status (Prioritized over subscription)
        if getattr(self.user, 'on_free_trial', False):
            return 500

        # Safe access to subscription in case user has none
        if not hasattr(self.user, 'subscription'):
            return 0
            
        sub_type = self.user.subscription.type
        if sub_type == 'basic':
            return 1000
        elif sub_type in ['warmup', 'unibox', 'premium']:
            return 3000
        return 0 # Default fallback

    def __str__(self):
        return f"{self.user.username} - Used: {self.used_count}"


class VerificationBatch(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='verification_batches')
    
    # Just for display in the history table (e.g., "leads_october.xlsx")
    original_filename = models.CharField(max_length=255)

    # to store the exact order of columns from the uploaded file 
    original_headers = models.JSONField(default=list)
    
    # The intermediate JSON file (input for the worker). We will delete this after processing is done
    clean_data_file = models.FileField(upload_to='verification_staging/', null=True, blank=True)
    
    # The final result (CSV) for the user to download
    output_file = models.FileField(upload_to='verification_results/', null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Verification Batch"
        verbose_name_plural = "Verification Batches"

    def __str__(self):
        return f"{self.original_filename} ({self.status})"

    @property
    def is_downloadable(self):
        return self.status == 'COMPLETED' and self.output_file

