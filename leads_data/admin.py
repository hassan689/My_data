from django.contrib import admin
from .models import Lead

# ✅ Customizing Lead Admin
@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("mc_number", "legal_name", "email", "status", "drivers")
    search_fields = ("mc_number", "legal_name", "email", "status")
    list_filter = ("status", "carrier_operation", "operation_classification")
    ordering = ("-mc_number",)
