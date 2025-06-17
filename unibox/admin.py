from django.contrib import admin
from .models import *
from django.db.models import Count, F, ExpressionWrapper, IntegerField


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


@admin.register(IncomingEmailMessage)
class IncomingEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'sender', 'recipient', 'thread', 'received_at')
    search_fields = ('subject', 'sender', 'recipient', 'message_id')
    list_filter = ('received_at',)

@admin.register(OutgoingEmailMessage)
class OutgoingEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'sender', 'recipient', 'thread', 'sent_at')
    search_fields = ('subject', 'sender', 'recipient', 'message_id')
    list_filter = ('sent_at',)




