from django.contrib import admin
from .models import WarmupProfile, DailyStat, WarmupEmail

@admin.register(WarmupProfile)
class WarmupProfileAdmin(admin.ModelAdmin):
    list_display = (
        "get_user",
        "email_account", 
        "status", 
        "warmup_enabled", 
        "current_daily", 
        "health_score", 
        "warmup_age"
    )
    list_filter = ("status", "warmup_enabled")
    search_fields = (
        "email_account__email_address", 
        "email_account__user__email", 
        "email_account__user__username"
    )

    def get_user(self, obj):
        return obj.email_account.user.username
    get_user.short_description = 'User'
    get_user.admin_order_field = 'email_account__user'


@admin.register(DailyStat)
class DailyStatAdmin(admin.ModelAdmin):
    list_display = ("profile", "date", "sent", "received", "inbox", "spam", "replied")
    list_filter = ("date",)
    search_fields = ("profile__email_account__email_address",)
    ordering = ("-date",)


@admin.register(WarmupEmail)
class WarmupEmailAdmin(admin.ModelAdmin):
    list_display = ("sender", "recipient", "subject", "status", "sent_at")
    list_filter = ("status", "sent_at")
    search_fields = (
        "sender__email_address",
        "recipient__email_address",
        "message_id",
        "subject",
    )
    ordering = ("-sent_at",)

