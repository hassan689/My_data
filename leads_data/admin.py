from django.contrib import admin
from .models import Lead, DailySheet

# ✅ Customizing Lead Admin
@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("mc_number", "legal_name", "status", "drivers", "added_on")
    search_fields = ("mc_number", "legal_name", "status", "added_on")
    list_filter = ("status", "carrier_operation", "operation_classification")
    ordering = ("-mc_number",)


@admin.register(DailySheet)
class DailySheetAdmin(admin.ModelAdmin):
    list_display = ("file", "uploaded_at")
    ordering = ("-uploaded_at",)

