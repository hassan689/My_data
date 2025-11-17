from django.db import models
from users.models import EmailAccount, CustomUser
from datetime import timedelta


class DripCampaign(models.Model):
    
    name = models.CharField(max_length=255)
    launched_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    
    min_delay = models.IntegerField(default=0, null=True, blank=True)
    max_delay = models.IntegerField(default=0, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True) 
    step_delay = models.DurationField(default=timedelta(days=1)) # Gap between steps

    current_step = models.IntegerField(default=1) 
    next_action_at = models.DateTimeField(null=True, blank=True) 
    last_action_at = models.DateTimeField(null=True, blank=True)
    
    # need a json field to store the MC numbers that were removed from the recipients list due to bounces/unsubscribes
    removed_mc_numbers = models.JSONField(default=list, null=True, blank=True)

    CAMPAIGN_STATUS_CHOICES = [
        ('Active', 'Active'),       
        ('Processing', 'Processing'), 
        ('Paused', 'Paused'),       
        ('Completed', 'Completed'),   
        ('Failed', 'Failed'),
        ('Cancelled', 'Cancelled'),
    ]
    status = models.CharField(max_length=20, choices=CAMPAIGN_STATUS_CHOICES, null=True, blank=True)
    LEAD_SOURCE_CHOICES = [
        ('Excel', 'Excel'),
        ('DB', 'DB'),
    ]
    lead_source = models.CharField(max_length=10, choices=LEAD_SOURCE_CHOICES, null=True, blank=True)
    total_recipients = models.IntegerField(default=0)

    def __str__(self):
        return f"Campaign {self.name} launched by {self.launched_by}"

    class Meta:
        verbose_name = "Drip Campaign"
        verbose_name_plural = "Drip Campaigns"


class EmailAccountAndLeads(models.Model):
    
    campaign = models.ForeignKey(DripCampaign, on_delete=models.CASCADE, related_name="email_accounts_and_leads")
    email_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE)
    leads_data = models.JSONField(default=list) # Stores list of lead emails as JSON

    recipient_count = models.IntegerField(default=0) # Number of leads associated with this email account for the latest step.
    sent_count = models.IntegerField(default=0) # Number of emails sent from this account in this campaign for the latest step.

    last_reply_check_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="The last time we checked this account's IMAP for replies for this campaign."
    )
    ACCOUNT_STATUS_CHOICES = [
        ('Ready', 'Ready'),
        ('Processing', 'Processing'),
        ('Stopped', 'Stopped'),
        ('Completed', 'Completed'),
    ]
    status = models.CharField(
        max_length=20, 
        choices=ACCOUNT_STATUS_CHOICES, 
        default='Ready'
    )

    def __str__(self):
        return f"Email Account {self.email_account.id} for Drip Campaign {self.campaign.id}"
    
    class Meta:
        verbose_name = "Email Account and Leads"
        verbose_name_plural = "Email Accounts and Leads"


class DripTemplate(models.Model):
    
    campaign = models.ForeignKey(DripCampaign, on_delete=models.CASCADE, related_name="templates")
    step_number = models.IntegerField() # Order of this step in the drip campaign
    subject = models.CharField(max_length=255, null=True, blank=True)
    body = models.TextField(null=True, blank=True)
    
    track_template = models.BooleanField(default=False) # Individually track opens for this template
    open_rate = models.IntegerField(default=0)

    DELIVERED_STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Processing', 'Processing'),
        ('Sent', 'Sent'),
        ('Failed', 'Failed'),
        ('Cancelled', 'Cancelled'),
    ]
    delivered_status = models.CharField(max_length=20, choices=DELIVERED_STATUS_CHOICES, default='Pending')

    def __str__(self):
        return f"Step {self.step_number} of Drip Campaign {self.campaign.id}"
    
    class Meta:
        unique_together = ('campaign', 'step_number')
        ordering = ['step_number']
        verbose_name = "Drip Template"
        verbose_name_plural = "Drip Templates"


class SentDripEmail(models.Model):
    """
    This is the 'paper trail'. It links a sent Message-ID
    to a specific campaign and lead.
    """
    
    drip_campaign = models.ForeignKey(
        DripCampaign, 
        on_delete=models.CASCADE, 
        related_name="sent_emails"
    )
    template = models.ForeignKey(
        DripTemplate, 
        on_delete=models.CASCADE, 
        related_name="sent_logs",
        null=True,
        blank=True
    )
    # The unique ID we sent in the email header
    message_id = models.CharField(
        max_length=255, 
        unique=True, 
        db_index=True
    )
    # The lead's info, for easy removal later
    lead_email = models.EmailField()
    lead_mc_number = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        db_index=True
    )
    STATUS_CHOICES = [
        ('Sent', 'Sent'),
        ('Replied', 'Replied'),
    ]
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='Sent'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.lead_email} - {self.message_id}"

