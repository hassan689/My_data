from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, EmailAccount
from django.forms import TextInput
from django.db import models

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
@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "email_address", "is_active", "last_used_at")
    list_filter = ("is_active", "last_used_at")
    search_fields = ("email_address", "user__username")

    # Override form fields to use TextField
    formfield_overrides = {
        models.TextField: {"widget": TextInput(attrs={"size": "40"})}, 
    }
