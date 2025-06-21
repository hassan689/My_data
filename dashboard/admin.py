from django.contrib import admin
from .models import GmailToken, CampaignRecord

@admin.register(GmailToken)
class GmailTokenAdmin(admin.ModelAdmin):
    list_display = ('email_account', 'created_at', 'expires_in', 'scope')
    readonly_fields = ('access_token', 'refresh_token', 'token_type', 'scope', 'created_at')


@admin.register(CampaignRecord)
class CampaignRecordAdmin(admin.ModelAdmin):
    list_display = ('launched_by', 'sender_account', 'launch_time', 'total_recipients')
    list_filter = ("launch_time",)
    search_fields = ("launched_by",)


