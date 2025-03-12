from django.contrib import admin
from .models import Subscription

# ✅ Customizing Subscription Admin
@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "start_date", "end_date", "amount_paid", "status")
    search_fields = ("user__username", "user__email")
    list_filter = ("status",)
    ordering = ("-start_date",)
    readonly_fields = ("amount_paid",)
