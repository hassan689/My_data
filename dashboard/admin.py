from django.contrib import admin
from .models import GmailToken, CampaignRecord, EmailOpen, VerificationBatch, VerificationUsage
from django.utils import timezone
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



@admin.register(CampaignRecord)
class CampaignRecordAdmin(admin.ModelAdmin):
    list_display = ('launched_by', 'sender_account', 'display_launch_or_schedule_time', 'total_recipients', 'sent_count', 'status', 'lead_source')
    list_filter = ("launch_time", 'status',)
    search_fields = ("launched_by__username", "sender_account__email_address")

    def display_launch_or_schedule_time(self, obj):
        if obj.status == 'pending' and obj.scheduled_launch_time:
            return timezone.localtime(obj.scheduled_launch_time).strftime('%Y-%m-%d %H:%M %p (Scheduled)')
        elif obj.launch_time:
            return timezone.localtime(obj.launch_time).strftime('%Y-%m-%d %H:%M %p (Started)')
        return "-" # Fallback if no time is available

    display_launch_or_schedule_time.short_description = 'Launch/Schedule Time'
    display_launch_or_schedule_time.admin_order_field = 'launch_time' # Allows sorting by launch_time


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

