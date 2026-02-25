from django.contrib import admin
from django.urls import reverse
from .models import GmailToken, CampaignRecord, EmailOpen, VerificationBatch, VerificationUsage, CampaignTemplate
from django.utils import timezone
from django.db.models import Count
from django.utils.html import format_html

@admin.register(GmailToken)
class GmailTokenAdmin(admin.ModelAdmin):
    list_display = ('email_account', 'created_at')
    
    # Fields to display in the change form and their order.
    fields = (
        'email_account', 'masked_access_token', 'masked_refresh_token', 'expires_in', 'token_type', 'scope', 'created_at', 'last_history_id',
    )
    readonly_fields = (
        'email_account', 'masked_access_token', 'masked_refresh_token', 'expires_in', 'token_type', 'scope', 'created_at', 'last_history_id'
    )
    list_filter = ("created_at",)

    def masked_access_token(self, obj):
        """Returns a masked version of the access token."""
        if obj.access_token_encrypted:
            return "********"
        return "N/A"

    masked_access_token.short_description = "Access Token"

    def masked_refresh_token(self, obj):
        """Returns a masked version of the refresh token."""
        if obj.refresh_token_encrypted:
            return "********"
        return "N/A"

    masked_refresh_token.short_description = "Refresh Token"


# 1. Register the CampaignTemplate Model
@admin.register(CampaignTemplate)
class CampaignTemplateAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'owner', 'created_at', 'first_campaign_link')
    list_filter = ('created_at',)
    search_fields = ('owner', 'name', 'subject', 'body')
    readonly_fields = ('created_at', 'updated_at')

    def first_campaign_link(self, obj):
        # Fetch the first campaign associated with this template
        # "campaigns" is the related_name defined on CampaignRecord.templates
        first_campaign = obj.campaigns.first()
        
        if first_campaign:
            # Generate the URL for the CampaignRecord change page
            url = reverse("admin:dashboard_campaignrecord_change", args=[first_campaign.id])
            
            # Return a safe HTML link
            return format_html('<a href="{}">{} (ID: {})</a>', url, first_campaign, first_campaign.id)
        
        return "-" # Fallback if not used in any campaign yet

    first_campaign_link.short_description = "First Linked Campaign"


# 2. Update the Inline
class CampaignTemplateInline(admin.TabularInline):
    model = CampaignRecord.templates.through
    extra = 1
    verbose_name = "Associated Template"
    verbose_name_plural = "Associated Templates"
    
    # 'campaigntemplate' is the ForeignKey. By listing it in 'fields' without 
    # adding it to 'readonly_fields', it becomes an editable dropdown.
    fields = ('campaigntemplate', 'template_id_display')
    readonly_fields = ('template_id_display',)

    def template_id_display(self, instance):
        # Safely get ID (handle cases where row is new/unsaved)
        try:
            return instance.campaigntemplate.id
        except (AttributeError, CampaignTemplate.DoesNotExist):
            return "-"
    
    template_id_display.short_description = "Template ID"


# 3. Update CampaignRecord Admin with Dynamic Fields
@admin.register(CampaignRecord)
class CampaignRecordAdmin(admin.ModelAdmin):
    list_display = (
        'launched_by', 
        'sender_account',
        'scheduled_at_display',
        'started_at_display', 
        'total_recipients', 
        'sent_count', 
        'status', 
        'lead_source', 
        'template_count_display'
    )
    list_filter = ("launch_time", 'status',)
    search_fields = ("launched_by__username", "sender_account__email_address")
    
    exclude = ('templates',)
    inlines = [CampaignTemplateInline]

    # --- Dynamic Field Hiding Logic ---
    def get_fields(self, request, obj=None):
        """
        Dynamically hide 'subject' and 'body' if they are empty (V2 records).
        Show them if they have data (Legacy records).
        """
        # Get the default list of fields
        fields = list(super().get_fields(request, obj))
        
        # Only apply logic if we are editing an existing object
        if obj:
            # Check if legacy fields have data
            has_subject = bool(obj.subject and obj.subject.strip())
            has_body = bool(obj.body and obj.body.strip())
            
            # If both are empty, this is a V2 record -> Hide them
            if not has_subject and not has_body:
                if 'subject' in fields: fields.remove('subject')
                if 'body' in fields: fields.remove('body')
        
        return fields

    @admin.display(description='Scheduled at', ordering='scheduled_launch_time')
    def scheduled_at_display(self, obj):
        """
        Only show the schedule if the campaign is still pending.
        Once moved to processing or launched, we clear this 'phase'.
        """
        if obj.status == 'pending' and obj.scheduled_launch_time:
            return timezone.localtime(obj.scheduled_launch_time).strftime('%Y-%m-%d %H:%M %p')
        return None

    @admin.display(description='Started at', ordering='launch_time')
    def started_at_display(self, obj):
        """
        - Hide for pending.
        - If scheduled, show the scheduled time (the 'planned' start).
        - If instant, show the auto_now_add launch_time.
        """
        if obj.status == 'pending':
            return None
        
        # Determine the 'logical' start time
        # Priority 1: The time it was supposed to start
        # Priority 2: The time the record was created (instant launch)
        target_time = obj.scheduled_launch_time or obj.launch_time
        
        if target_time:
            return timezone.localtime(target_time).strftime('%Y-%m-%d %H:%M %p')
        return None

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(templates_count=Count('templates'))

    def template_count_display(self, obj):
        return obj.templates_count 

    template_count_display.short_description = 'Templates Used'
    template_count_display.admin_order_field = 'templates_count'


@admin.register(EmailOpen)
class EmailOpenAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'is_opened')
    list_filter = ('is_opened',)


@admin.register(VerificationUsage)
class VerificationUsageAdmin(admin.ModelAdmin):
    list_display = ('user', 'quota_status', 'next_reset_at', 'time_until_reset', 'total_batches_count')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('next_reset_at',)

    def quota_status(self, obj):
        # Shows "450 / 1000" with simple color coding
        limit = obj.get_limit()
        used = obj.used_count
        
        color = "green"
        if used >= limit:
            color = "red"
        elif used > (limit * 0.8):
            color = "orange"
            
        return format_html(
            '<span style="color: {}; font-weight: bold;">{} / {}</span>',
            color, used, limit
        )
    quota_status.short_description = "Usage / Limit"

    def total_batches_count(self, obj):
        # Just returns the number of files
        return obj.user.verification_batches.count()
    total_batches_count.short_description = "Total Files"

    def time_until_reset(self, obj):
        if not obj.next_reset_at:
            return "-"
        
        now = timezone.now()
        if now >= obj.next_reset_at:
            return "Ready to Reset"
        
        diff = obj.next_reset_at - now
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"
    time_until_reset.short_description = "Reset In"


@admin.register(VerificationBatch)
class VerificationBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'original_filename', 'status_badge', 'created_at', 'download_output')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'original_filename', 'user__email')
    actions = ['mark_as_failed', 'retry_processing']

    def status_badge(self, obj):
        colors = {
            'COMPLETED': 'green',
            'PROCESSING': 'blue',
            'PENDING': 'gray',
            'FAILED': 'red',
        }
        color = colors.get(obj.status, 'black')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 10px; font-size: 10px;">{}</span>',
            color, obj.status
        )
    status_badge.short_description = "Status"

    def download_output(self, obj):
        if obj.output_file:
            return format_html('<a href="{}" download>Download CSV</a>', obj.output_file.url)
        return "-"
    download_output.short_description = "Result"

    # --- Actions ---
    @admin.action(description='Mark selected batches as FAILED')
    def mark_as_failed(self, request, queryset):
        queryset.update(status='FAILED')

    @admin.action(description='Reset to PENDING (Retry)')
    def retry_processing(self, request, queryset):
        queryset.update(status='PENDING')
        self.message_user(request, "Selected batches reset to PENDING.")

