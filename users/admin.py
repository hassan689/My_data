from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, EmailAccount
from django.forms import PasswordInput
from django.db import models
from django import forms

# ✅ Customizing CustomUser Admin
@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "email", 
        "phone_number", "company_name", "mc_number", 
        "on_free_trial", "lifetime_value", "months_subscribed"
    )
    search_fields = ("username", "email", "first_name", "last_name", "mc_number", "phone_number")
    list_filter = ("on_free_trial",)
    ordering = ("-date_joined",)

    fieldsets = (
        ("Personal Information", {"fields": ("username", "first_name", "last_name", "email", "phone_number")}),
        ("Company Details", {"fields": ("company_name", "website_link", "mc_number")}),
        ("Subscription Info", {"fields": ("on_free_trial", "lifetime_value", "months_subscribed")}),
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

    readonly_fields = ("lifetime_value", "months_subscribed")


# ✅ Customizing EmailAccount Admin
class EmailAccountForm(forms.ModelForm):
    decrypted_password = forms.CharField(
        required=False, 
        widget=PasswordInput(render_value=True)  # Show password but keep it masked
    )

    class Meta:
        model = EmailAccount
        fields = ("user", "email_address", "decrypted_password", "is_active", "last_used_at")

    def __init__(self, *args, **kwargs):
        """Auto-fill decrypted password when editing an email account."""
        super().__init__(*args, **kwargs)
        if self.instance.pk:  # Only try decrypting if an instance exists
            try:
                self.fields["decrypted_password"].initial = self.instance.get_password()
            except Exception as e:
                print(f"Decryption error: {e}")  # Debugging purposes
                self.fields["decrypted_password"].initial = "ERROR: Unable to decrypt"

    def save(self, commit=True):
        """Ensure password gets encrypted properly if changed."""
        email_account = super().save(commit=False)

        # Only update password if the decrypted field is changed
        if self.cleaned_data.get("decrypted_password"):
            email_account.set_password(self.cleaned_data["decrypted_password"])

        if commit:
            email_account.save()
        return email_account


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    form = EmailAccountForm  # Use custom form with decryption
    list_display = ("user", "email_address", "is_active", "last_used_at")
    list_filter = ("is_active", "last_used_at")
    search_fields = ("email_address", "user__username")

    def get_form(self, request, obj=None, **kwargs):
        """Ensure correct form is used when editing an existing entry."""
        kwargs["form"] = EmailAccountForm
        return super().get_form(request, obj, **kwargs)

