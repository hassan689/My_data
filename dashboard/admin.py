from django.contrib import admin
from .models import OutgoingEmailMessage, IncomingEmailMessage

@admin.register(OutgoingEmailMessage)
class OutgoingEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('subject', 'sender', 'recipient', 'sent_at', 'thread')
    search_fields = ('subject', 'sender', 'recipient', 'message_id')
    list_filter = ('sent_at', 'thread')
    readonly_fields = ('message_id', 'sent_at')


@admin.register(IncomingEmailMessage)
class IncomingEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('subject', 'sender', 'received_at', 'thread')
    search_fields = ('subject', 'sender', 'message_id')
    list_filter = ('received_at', 'thread')
    readonly_fields = ('message_id', 'received_at')

