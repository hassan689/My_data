from django.contrib import admin
from .models import EmailThread
import django_mailbox.admin
from django.db.models import Count, F, ExpressionWrapper, IntegerField
from django.db import models



@admin.register(EmailThread)
class EmailThreadAdmin(admin.ModelAdmin):
    list_display = ('id', 'mailbox', 'subject', 'email1', 'email2', 'total_messages', 'is_read')
    search_fields = ('subject', 'email1', 'email2')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.annotate(
            num_incoming=Count('incoming_messages'),
            num_outgoing=Count('outgoing_messages'),
            total_msgs=ExpressionWrapper(
                F('num_incoming') + F('num_outgoing'),
                output_field=IntegerField()
            )
        )

    def total_messages(self, obj):
        return obj.total_msgs

    total_messages.admin_order_field = 'total_msgs'
    total_messages.short_description = 'Total Messages'

