from django.contrib import admin
from .models import Subscription

# ✅ Customizing Subscription Admin
@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "company_name", "start_date", "end_date", "status", "renewal_count")
    search_fields = ("user__username", "user__email")
    list_filter = ("status",)
    ordering = ("-start_date",)
    readonly_fields = ("renewal_count",)
    list_editable = ("status",)
    
    def company_name(self, obj):
        return obj.user.company_name  # Accessing company_name from related CustomUser model

    company_name.admin_order_field = "user__company_name"  # Allows sorting by company_name
    company_name.short_description = "Company Name"  # Sets a readable column name in the admin panel
