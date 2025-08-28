from django.contrib import admin
from .models import Subscription, Revenue
from django.db.models import F, ExpressionWrapper, DecimalField

class ReferredUserFilter(admin.SimpleListFilter):
    title = 'Referred User'  # Human-readable title
    parameter_name = 'referred_user'  # URL parameter

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Yes'),
            ('no', 'No'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(user__referred_by__isnull=True)
        if self.value() == 'no':
            return queryset.filter(user__referred_by__isnull=True)

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "affiliate", "start_date", "end_date", "status", "type", "renewal_count", "paid_amount",)
    search_fields = ("user__username", "user__email")
    list_filter = ("status", "type", ReferredUserFilter,)
    ordering = ("-start_date",)
    readonly_fields = ("renewal_count",)
    list_editable = ("status", "paid_amount", "type",)
    
    def company_name(self, obj):
        return obj.user.company_name

    company_name.admin_order_field = "user__company_name"
    company_name.short_description = "Company Name"

    def affiliate(self, obj):
        return obj.user.referred_by



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


