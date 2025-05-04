from django.db import models
from users.models import EmailAccount
from django.db.models import Value, CharField, F


class EmailThread(models.Model):
    email_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="threads")
    subject = models.CharField(max_length=255)
    started_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    def __str__(self):
        return f"Thread: {self.subject} - ({self.email_account.email_address})"
    
    def get_ordered_messages(self):
        incoming = self.incoming_messages.annotate(
            timestamp=F('received_at'),
            direction=Value('incoming', output_field=CharField()),
            recipient=Value('', output_field=CharField())  # dummy field to match outgoing
        ).values(
            'id', 'subject', 'body', 'sender', 'recipient',
            'message_id', 'in_reply_to', 'timestamp', 'direction'
        )

        outgoing = self.outgoing_messages.annotate(
            timestamp=F('sent_at'),
            direction=Value('outgoing', output_field=CharField())
        ).values(
            'id', 'subject', 'body', 'sender', 'recipient',
            'message_id', 'in_reply_to', 'timestamp', 'direction'
        )
        result = incoming.union(outgoing, all=True).order_by('timestamp')

        return result



