from django.contrib import admin
from .models import Lead, DailySheet, SkipList

# ✅ Customizing Lead Admin
@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("mc_number", "legal_name", "city", "state", "drivers", "added_on")
    search_fields = ("mc_number", "legal_name", "status", "added_on")
    list_filter = ("status", "carrier_operation", "operation_classification")
    ordering = ("-mc_number",)


@admin.register(DailySheet)
class DailySheetAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "row_count", "uploaded_at")
    ordering = ("-uploaded_at",)


@admin.register(SkipList)
class SkipListAdmin(admin.ModelAdmin):
    list_display = ("user", "get_mc_count", "get_emails_count")

    @admin.display(description='Mc Skipped Count')
    def get_mc_count(self, obj):
        return len(obj.mc_numbers)
    
    @admin.display(description='Emails Skipped Count')
    def get_emails_count(self, obj):
        return len(obj.emails)

