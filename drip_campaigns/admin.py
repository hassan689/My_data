from django.contrib import admin
from django.db.models import Count
from .models import DripCampaign, EmailAccountAndLeads, DripTemplate

# --- Inlines for the DripCampaign Admin ---

class DripTemplateInline(admin.TabularInline):
    """
    Allows viewing and editing templates directly 
    from the DripCampaign detail page.
    """
    model = DripTemplate
    fields = ('step_number', 'subject', 'delivered_status', 'track_template', 'open_rate')
    extra = 1 # Show one empty slot for a new template
    ordering = ('step_number',)


# --- Main Model Admins ---

@admin.register(DripCampaign)
class DripCampaignAdmin(admin.ModelAdmin):
    """
    Admin configuration for the main DripCampaign model.
    """
    list_display = (
        'name', 
        'launched_by', 
        'status', 
        'current_step', 
        'template_count', # Your requested field
        'next_action_at', 
        'created_at'
    )
    list_filter = ('status', 'lead_source')
    search_fields = ('name', 'launched_by__username')
    inlines = [DripTemplateInline]
    
    # Add a read-only field for the creation time
    readonly_fields = ('created_at',)
    
    fieldsets = (
        (None, {
            'fields': ('name', 'launched_by', 'status', 'total_recipients', 'created_at')
        }),
        ('Scheduling', {
            'fields': ('step_delay', 'current_step', 'next_action_at', 'last_action_at')
        }),
        ('Settings', {
            'fields': ('min_delay', 'max_delay', 'lead_source')
        }),
    )

    def get_queryset(self, request):
        """
        Optimize the queryset by annotating it with the template count.
        """
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(
            _template_count=Count('templates', distinct=True)
        )
        return queryset

    @admin.display(description='Templates', ordering='_template_count')
    def template_count(self, obj):
        """
        Returns the annotated count of templates for the list display.
        """
        return obj._template_count


@admin.register(EmailAccountAndLeads)
class EmailAccountAndLeadsAdmin(admin.ModelAdmin):
    """
    Admin for viewing the EmailAccountAndLeads model directly.
    """
    list_display = ('campaign', 'email_account', 'recipient_count', 'sent_count')
    search_fields = ('campaign__name', 'email_account__email_address')
    # Make leads_data read-only as it can be very large
    readonly_fields = ('leads_data',) 


@admin.register(DripTemplate)
class DripTemplateAdmin(admin.ModelAdmin):
    """
    Admin for viewing the DripTemplate model directly.
    """
    list_display = ('campaign', 'step_number', 'subject', 'delivered_status', 'open_rate')
    list_filter = ('delivered_status',)
    search_fields = ('subject', 'campaign__name')

