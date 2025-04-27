from django.contrib import admin
from .models import EmailThread
import django_mailbox.admin


@admin.register(EmailThread)
class EmailThreadAdmin(admin.ModelAdmin):
    list_display = ('subject', 'started_at', 'email_account')
    search_fields = ('subject', 'email_account__email_address')
    list_filter = ('started_at', 'subject')

