from django.contrib import admin
from .models import GmailToken, CampaignRecord
from django.utils import timezone

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



@admin.register(CampaignRecord)
class CampaignRecordAdmin(admin.ModelAdmin):
    list_display = ('launched_by', 'sender_account', 'display_launch_or_schedule_time', 'total_recipients', 'sent_count', 'status', 'lead_source')
    list_filter = ("launch_time",)
    search_fields = ("launched_by",)

    def display_launch_or_schedule_time(self, obj):
        if obj.status == 'pending' and obj.scheduled_launch_time:
            return timezone.localtime(obj.scheduled_launch_time).strftime('%Y-%m-%d %H:%M %p (Scheduled)')
        elif obj.launch_time:
            return timezone.localtime(obj.launch_time).strftime('%Y-%m-%d %H:%M %p (Completed)')
        return "-" # Fallback if no time is available

    display_launch_or_schedule_time.short_description = 'Launch/Schedule Time'
    display_launch_or_schedule_time.admin_order_field = 'launch_time' # Allows sorting by launch_time


