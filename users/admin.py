from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, EmailAccount, IMAPSettings
from django.forms import PasswordInput
from django import forms
from django.core.exceptions import ValidationError

# ✅ Customizing CustomUser Admin
@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "email",
        "company_name",
        "date_joined", 
        "on_free_trial",
        "email_account_count"
    )
    search_fields = ("username", "email", "first_name", "last_name", "company_name")
    list_filter = ("on_free_trial",)
    list_editable = ("on_free_trial",)
    ordering = ("-date_joined",)

    fieldsets = (
        ("Personal Information", {"fields": ("username", "first_name", "last_name", "email", "phone_number")}),
        ("Company Details", {"fields": ("company_name", "website_link", "mc_number")}),
        ("Subscription Info", {"fields": ("on_free_trial", )}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important Dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        ("Create User", {
            "classes": ("wide",),
            "fields": (
                "username", "email", "first_name", "last_name", "phone_number",
                "password1", "password2", "company_name", "website_link", "mc_number",
                "on_free_trial", "is_active", "is_staff", "is_superuser", "groups", "user_permissions"
            ),
        }),
    )

    def email_account_count(self, obj):
        return obj.email_accounts.count()
    email_account_count.short_description = "No. of Accounts"


# ✅ Customizing EmailAccount Admin
class EmailAccountForm(forms.ModelForm):
    decrypted_password = forms.CharField(
        required=False, 
        widget=PasswordInput(render_value=True)  # Show password but keep it masked
    )

    class Meta:
        model = EmailAccount
        fields = ("user", "email_address", "decrypted_password", 
                  "email_provider", "port_number", "server_type", "host")

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


class IMAPSettingsInline(admin.StackedInline):
    model = IMAPSettings


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    form = EmailAccountForm  # Use custom form with decryption
    list_display = ("user", "company_name", "email_address", "email_provider", "has_imap_configured", "last_used_at")
    list_filter = ("last_used_at", "email_provider")
    search_fields = ("email_address", "user__username")

    # Include IMAPSettings as inline in EmailAccount admin form
    inlines = [IMAPSettingsInline]
    
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

