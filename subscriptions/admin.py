from django.contrib import admin
from .models import Subscription, Revenue
from django.db.models import F, ExpressionWrapper, DecimalField

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "company_name", "start_date", "end_date", "status", "renewal_count", "paid_amount",)
    search_fields = ("user__username", "user__email")
    list_filter = ("status",)
    ordering = ("-start_date",)
    readonly_fields = ("renewal_count",)
    list_editable = ("status", "paid_amount",)
    
    def company_name(self, obj):
        return obj.user.company_name  # Accessing company_name from related CustomUser model

    company_name.admin_order_field = "user__company_name"  # Allows sorting by company_name
    company_name.short_description = "Company Name"  # Sets a readable column name in the admin panel


@admin.register(Revenue)
class RevenueAdmin(admin.ModelAdmin):
    list_display = ("month_display", "net_revenue", "paid_to_affiliates", "calculated_total_revenue")
    ordering = ("-month",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Annotate total revenue as net_revenue + paid_to_affiliates
        return qs.annotate(
            total_rev=ExpressionWrapper(
                F("net_revenue") + F("paid_to_affiliates"),
                output_field=DecimalField(max_digits=10, decimal_places=2)
            )
        )

    def calculated_total_revenue(self, obj):
        return obj.total_rev
    calculated_total_revenue.short_description = "Total Revenue"
    calculated_total_revenue.admin_order_field = "total_rev"

    def month_display(self, obj):
        return obj.month.strftime("%B %Y")
    month_display.short_description = "Month"


