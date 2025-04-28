from django.contrib import admin
from .models import EmailThread
import django_mailbox.admin
from django.db.models import Count



@admin.register(EmailThread)
class EmailThreadAdmin(admin.ModelAdmin):
    list_display = ('subject', 'started_at', 'email_account', 'messages_count')
    search_fields = ('subject', 'email_account__email_address')
    list_filter = ('started_at', 'subject')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            incoming_count=Count('incoming_messages', distinct=True),
            outgoing_count=Count('outgoing_messages', distinct=True),
        )
        return qs

    @admin.display(ordering='incoming_count')  # <= important
    def messages_count(self, obj):
        incoming = getattr(obj, 'incoming_count', 0)
        outgoing = getattr(obj, 'outgoing_count', 0)
        return incoming + outgoing

