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
            'fields': ('name', 'launched_by', 'status', 'total_recipients', 'created_at', 'removed_mc_numbers')
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
    
    # 1. --- All your requested fields in list_display ---
    list_display = (
        'get_campaign_name',
        'get_launched_by',
        'get_email_address',
        'recipient_count',
        'sent_count',
        'get_campaign_status',
        'get_step_delay',
        'get_current_step',
        'get_last_action',
        'get_next_action',
        'get_lead_source',
    )

    # 2. --- Your requested list_filter ---
    list_filter = ('campaign__last_action_at', 'campaign__status')

    # 3. --- Search fields for related models ---
    search_fields = ('campaign__name', 'email_account__email_address', 'campaign__launched_by__email')
    
    # 4. --- Read-only field ---
    readonly_fields = ('leads_data',) 

    # 5. --- Query Optimization ---
    # This is critical for performance. It tells Django to
    # fetch the related models in a single, efficient query.
    list_select_related = ('campaign', 'email_account', 'campaign__launched_by')

    # --- Custom methods to get related data ---

    @admin.display(description='Campaign Name', ordering='campaign__name')
    def get_campaign_name(self, obj):
        return obj.campaign.name
    
    @admin.display(description='Email Account', ordering='email_account__email_address')
    def get_email_address(self, obj):
        return obj.email_account.email_address
    
    @admin.display(description='Campaign Status', ordering='campaign__status')
    def get_campaign_status(self, obj):
        return obj.campaign.get_status_display()

    @admin.display(description='Launched By', ordering='campaign__launched_by')
    def get_launched_by(self, obj):
        return obj.campaign.launched_by

    @admin.display(description='Step Delay', ordering='campaign__step_delay')
    def get_step_delay(self, obj):
        return obj.campaign.step_delay

    @admin.display(description='Current Step', ordering='campaign__current_step')
    def get_current_step(self, obj):
        return obj.campaign.current_step

    @admin.display(description='Last Action', ordering='campaign__last_action_at')
    def get_last_action(self, obj):
        return obj.campaign.last_action_at

    @admin.display(description='Next Action', ordering='campaign__next_action_at')
    def get_next_action(self, obj):
        return obj.campaign.next_action_at

    @admin.display(description='Lead Source', ordering='campaign__lead_source')
    def get_lead_source(self, obj):
        return obj.campaign.get_lead_source_display()

@admin.register(DripTemplate)
class DripTemplateAdmin(admin.ModelAdmin):
    """
    Admin for viewing the DripTemplate model directly.
    """
    list_display = ('campaign', 'step_number', 'subject', 'delivered_status', 'open_rate')
    list_filter = ('delivered_status',)
    search_fields = ('subject', 'campaign__name')

