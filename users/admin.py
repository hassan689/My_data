from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, EmailAccount, Affiliate, AccountGroup
from django.db.models import Count, Case, When, BooleanField
from django.forms import PasswordInput
from django import forms
from django.core.exceptions import ValidationError


class HasSubscriptionFilter(admin.SimpleListFilter):
    """
    Custom filter to filter users by whether they have 
    a subscription instance, based on the 'has_subscription_annotated'
    annotation in get_queryset.
    """
    title = 'Has Subscription' # Title for the filter sidebar
    parameter_name = 'has_subscription' # URL parameter

    def lookups(self, request, model_admin):
        """Returns the filter options."""
        return (
            ('yes', 'Yes'),
            ('no', 'No'),
        )

    def queryset(self, request, queryset):
        """Applies the filter to the queryset."""
        if self.value() == 'yes':
            return queryset.filter(has_subscription_annotated=True)
        if self.value() == 'no':
            return queryset.filter(has_subscription_annotated=False)
        return queryset


# ✅ Customizing CustomUser Admin
@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "phone_number",
        "date_joined",
        "on_free_trial",
        "attached_accounts_count",
        "has_subscription",
        "subscription__status",
        "referred_by",
    )
    search_fields = ("username", "first_name", "last_name", "company_name")
    list_filter = ("on_free_trial", "date_joined", HasSubscriptionFilter,)
    list_editable = ("on_free_trial",)
    ordering = ("-date_joined",)

    fieldsets = (
        ("Personal Information", {"fields": ("username", "first_name", "last_name", "email", "phone_number", "referred_by")}),
        ("Company Details", {"fields": ("company_name", "website_link", "mc_number")}),
        ("Subscription Info", {"fields": ("on_free_trial", )}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important Dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        ("Create User", {
            "classes": ("wide",),
            "fields": (
                "username", "email", "first_name", "last_name", "phone_number", "referred_by",
                "password1", "password2", "company_name", "website_link", "mc_number",
                "on_free_trial", "is_active", "is_staff", "is_superuser", "groups", "user_permissions"
            ),
        }),
    )

    @admin.display(boolean=True, description='Has Subscription', ordering='has_subscription_annotated')
    def has_subscription(self, obj):
        """Checks if the user has a subscription instance via annotation."""
        return obj.has_subscription_annotated

    @admin.display(description='Accounts', ordering='attached_accounts_count_annotated')
    def attached_accounts_count(self, obj):
        """
        Calculates and returns the number of email accounts associated with the user.
        """
        if hasattr(obj, 'attached_accounts_count_annotated'):
            return obj.attached_accounts_count_annotated
        return obj.email_accounts.count() 

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(
            attached_accounts_count_annotated=Count('email_accounts'),
            has_subscription_annotated=Case(
                When(subscription__isnull=False, then=True),
                default=False,
                output_field=BooleanField()
            )
        )
        return queryset



@admin.register(Affiliate)
class AffiliateAdmin(admin.ModelAdmin):
    
    list_display = ("user", "display_commission_percentage", 
                    "lifetime_earnings", "has_been_paid", "pending_amount_display",
                    "referred_users_count",)
    
    list_filter = ("joining_date",)
    search_fields = ("user",)
    ordering = ("-joining_date",)
    readonly_fields = ('joining_date',)

    @admin.display(description='Referred Users Count', ordering='referred_users_count')
    def referred_users_count(self, obj):
        return obj.referred_users_count

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(referred_users_count=Count('referred_users'))
        return queryset
    
    @admin.display(description='Commission %', ordering='commission_percentage') # Sort by the raw field
    def display_commission_percentage(self, obj):
        if obj.commission_percentage is not None:
            return f"{obj.commission_percentage}%"
        return "N/A"

    def pending_amount_display(self, obj):
        return obj.pending_amount
    pending_amount_display.short_description = 'Pending Amount'


class EmailAccountForm(forms.ModelForm):
    decrypted_password = forms.CharField(
        required=False, 
        widget=PasswordInput(render_value=True)  # Show password but keep it masked
    )

    class Meta:
        model = EmailAccount
        fields = ("user", "email_address", "decrypted_password", "account_group",
                  "email_provider", "port_number", "server_type", "host", "is_warmup_target", "black_list")

    def __init__(self, *args, **kwargs):
        """Auto-fill decrypted password when editing an email account."""
        super().__init__(*args, **kwargs)
        if self.instance.id:  # Only try decrypting if an instance exists
            try:
                self.fields["decrypted_password"].initial = self.instance.get_password()
            except Exception as e:
                print(f"Decryption error: {e}")  # Debugging purposes
                self.fields["decrypted_password"].initial = "ERROR: Unable to decrypt"

    def save(self, commit=True):
        """Ensure password gets encrypted properly if changed."""
        email_account = super().save(commit=False)

        if self.cleaned_data.get("decrypted_password"):  # Ensure password is not empty
            email_account.set_password(self.cleaned_data["decrypted_password"])

        if commit:
            email_account.save()
        return email_account


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    form = EmailAccountForm  # Use custom form with decryption
    list_display = ("user", "company_name", "email_address", "is_warmup_target", "black_list", "last_used_at")
    list_filter = ("last_used_at", "is_warmup_target", "email_provider", "black_list")
    search_fields = ("email_address", "user__username")
    
    def company_name(self, obj):
        return obj.user.company_name  # Accessing company_name from related CustomUser model

    company_name.admin_order_field = "user__company_name"  # Allows sorting by company_name
    company_name.short_description = "Company Name"  # Sets a readable column name in the admin panel

    def get_form(self, request, obj=None, **kwargs):
        """Ensure correct form is used when editing an existing entry."""
        kwargs["form"] = EmailAccountForm
        return super().get_form(request, obj, **kwargs)
				
    def save_model(self, request, obj, form, change):
        try:
            obj.clean()  # Explicitly call clean() before saving
            obj.save()
        except ValidationError as e:
            self.message_user(request, e.messages[0], level=messages.ERROR)


@admin.register(AccountGroup)
class AccountGrouAdmin(admin.ModelAdmin):
    
    list_display = ('user', 'name', 'created_at')
    ordering = ('-created_at',)

