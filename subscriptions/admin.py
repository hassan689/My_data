from django.contrib import admin
from .models import Subscription, Revenue, Expense
from django.db.models import F, ExpressionWrapper, DecimalField, Sum, Subquery, OuterRef
from decimal import Decimal
from users.models import CustomUser

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


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('name', 'amount', 'created_at')
    search_fields = ('name',)


@admin.register(Revenue)
class RevenueAdmin(admin.ModelAdmin):
    list_display = ("month_display", "calculated_total_revenue", "paid_to_affiliates", "total_expenses","net_revenue", "fifty_percent_split")
    ordering = ("-month",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        
        # Annotate with total expenses for each month
        expenses_subquery = Expense.objects.filter(
            rev_month_id=OuterRef('id')
        ).values('rev_month_id').annotate(
            total_expenses=Sum('amount')
        ).values('total_expenses')

        return qs.annotate(
            total_expenses=Subquery(expenses_subquery, output_field=DecimalField()),
            total_rev=ExpressionWrapper(
                F("net_revenue") + F("paid_to_affiliates") + F("total_expenses"),
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )
        )

    def total_expenses(self, obj):
        return obj.total_expenses if obj.total_expenses is not None else Decimal('0.00')
    total_expenses.short_description = "Expenses"
    total_expenses.admin_order_field = "total_expenses"

    def calculated_total_revenue(self, obj):
        return obj.total_rev
    calculated_total_revenue.short_description = "Total Revenue"
    calculated_total_revenue.admin_order_field = "total_rev"

    def fifty_percent_split(self, obj):
        """Calculates 50% of the net revenue."""
        return (obj.net_revenue / Decimal('2.00')).quantize(Decimal('0.01'))
    fifty_percent_split.short_description = "50% Split"
    fifty_percent_split.admin_order_field = "net_revenue"

    def month_display(self, obj):
        return obj.month.strftime("%B %Y")
    month_display.short_description = "Month"
