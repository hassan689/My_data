from django.contrib import admin
from .models import OutgoingEmailMessage, IncomingEmailMessage


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

