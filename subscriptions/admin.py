from django.contrib import admin
from .models import Subscription

# ✅ Customizing Subscription Admin
@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "start_date", "status", "renewal_count")
    search_fields = ("user__username", "user__email")
    list_filter = ("status",)
    ordering = ("-start_date",)
    readonly_fields = ("renewal_count",)

# list editable = status
