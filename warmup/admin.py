from django.contrib import admin
from .models import *


admin.site.register(WarmupTemplateSet)

@admin.register(WarmupCampaign)
class WarmupCampaignAdmin(admin.ModelAdmin):
    list_display = ("sender_account__user", "sender_account", "status", "current_step", "last_action_at", "next_action_at")
    list_filter = ("status", "created_at")
    search_fields = ("sender_account__email_address",)
    ordering = ("-created_at",)

@admin.register(WarmupMessage)
class WarmupMessageAdmin(admin.ModelAdmin):
    list_display = ("campaign", "sender", "recipient", "subject", "sent_at")
    list_filter = ("sent_at",)
    search_fields = (
        "sender__email_address",
        "recipient__email_address",
        "subject",
        "body",
    )
    ordering = ("-sent_at",)

