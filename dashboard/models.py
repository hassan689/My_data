from django.db import models
from unibox.models import EmailThread

class OutgoingEmailMessage(models.Model):
    thread = models.ForeignKey(EmailThread, on_delete=models.CASCADE, related_name="outgoing_messages")
    subject = models.CharField(max_length=255)
    body = models.TextField()
    recipient = models.EmailField()
    sender = models.EmailField()
    message_id = models.CharField(max_length=255, unique=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    in_reply_to = models.CharField(max_length=255, null=True, blank=True)  # Tracks which incoming email this is replying to

    def __str__(self):
        return f"Sent: {self.subject} to {self.recipient}"
    
    class Meta:
        verbose_name = "Outgoing Message"
        verbose_name_plural = "Outgoing Messages"


class IncomingEmailMessage(models.Model):
    thread = models.ForeignKey(EmailThread, on_delete=models.CASCADE, related_name="incoming_messages")
    subject = models.CharField(max_length=255)
    body = models.TextField()
    sender = models.EmailField()
    recipient = models.EmailField()
    message_id = models.CharField(max_length=255, unique=True)
    in_reply_to = models.CharField(max_length=255, null=True, blank=True)  # Tracks which outgoing email this is replying to
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Incoming: {self.subject} from {self.sender}"

    class Meta:
        verbose_name = "Incoming Message"
        verbose_name_plural = "Incoming Messages"

