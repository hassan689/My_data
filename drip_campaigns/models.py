from django.db import models
from users.models import EmailAccount, CustomUser


# The idea will be that the DripCampaign will have a 

class DripCampaign(models.Model):
    
    sender_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="drip_campaigns")
    launched_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True)
    
    min_delay = models.IntegerField(default=0, null=True, blank=True)
    max_delay = models.IntegerField(default=0, null=True, blank=True)
    
    last_action_at = models.DateTimeField(null=True, blank=True) # can be calculated by the last sent template's sent_at
    next_action_at = models.DateTimeField(null=True, blank=True) # can be calculated by the next pending template's scheduled_launch_time
    
    created_at = models.DateTimeField(auto_now_add=True)
    total_recipients = models.IntegerField(default=0) # Total number of leads
    current_step = models.IntegerField(default=0) # calculated by the number of associated templates

    CAMPAIGN_STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Active', 'Active'),
        ('Complete', 'Complete'),
        ('Failed', 'Failed'),
    ]
    status = models.CharField(max_length=20, choices=CAMPAIGN_STATUS_CHOICES, null=True, blank=True)
    LEAD_SOURCE_CHOICES = [
        ('Excel', 'Excel'),
        ('DB', 'DB'),
    ]
    lead_source = models.CharField(max_length=10, choices=LEAD_SOURCE_CHOICES, null=True, blank=True)
    leads_data = models.JSONField(default=list, null=True, blank=True) # Stores the list of leads as JSON for this tem

    def __str__(self):
        return f"Launched by {self.launched_by} via {self.sender_account} at {self.launch_time.strftime('%Y-%m-%d %H:%M')}"

    class Meta:
        verbose_name = "Drip Campaign"
        verbose_name_plural = "Drip Campaigns"


class DripTemplate(models.Model):
    
    campaign = models.ForeignKey(DripCampaign, on_delete=models.CASCADE, related_name="templates")
    step_number = models.IntegerField()
    body = models.JSONField(default=list) # Stores body for this step as JSON: {"subject": "...", "body": "..."}
    
    track_template = models.BooleanField(default=False) # Individually track opens for this template
    open_rate = models.IntegerField(default=0)
    
    scheduled_launch_time = models.DateTimeField() # When this step is set to launch

    DELIVERED_STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Sent', 'Sent'),
        ('Failed', 'Failed'),
    ]
    delivered_status = models.CharField(max_length=20, choices=DELIVERED_STATUS_CHOICES, default='Pending')

    def __str__(self):
        return f"Step {self.step_number} of Drip Campaign {self.campaign.id}"
    
    class Meta:
        verbose_name = "Drip Template"
        verbose_name_plural = "Drip Templates"


