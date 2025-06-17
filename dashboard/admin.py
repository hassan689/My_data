from django.contrib import admin
from .models import GmailToken

@admin.register(GmailToken)
class GmailTokenAdmin(admin.ModelAdmin):
    list_display = ('email_account', 'created_at', 'expires_in', 'scope')
    readonly_fields = ('access_token', 'refresh_token', 'token_type', 'scope', 'created_at')

