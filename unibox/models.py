from django.db import models
from dashboard.models import GmailToken
from itertools import chain


class EmailThread(models.Model):
    mailbox = models.ForeignKey(GmailToken, on_delete=models.CASCADE)  # Owner
    email1 = models.EmailField()  # sender role
    email2 = models.EmailField()  # recipient role, which is mostly my mailbox's address
    subject = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    # email1 and email2 fields are only there to keep track of the participants in the conversation not the sender/reciver logics .....
    # incoming and outgoing message instances have the correct record of the sender and the recipient

    def __str__(self):
        return f"{self.email2} -> {self.subject}" # email 2 is favoured to be the owner of the thread, since its on the receiving side for the inboxes

    def get_ordered_messages(self):
        incoming = list(self.incoming_messages.all())
        outgoing = list(self.outgoing_messages.all())

        # Annotate each with a common timestamp attribute for sorting
        for msg in incoming:
            msg._timestamp = msg.received_at
        for msg in outgoing:
            msg._timestamp = msg.sent_at

        combined = list(chain(incoming, outgoing))
        combined.sort(key=lambda msg: msg._timestamp)

        return combined


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



class Attachment(models.Model):
    
    incoming_message = models.ForeignKey(
        IncomingEmailMessage,
        on_delete=models.CASCADE,
        related_name='attachments',
        null=True,
        blank=True
    )
    outgoing_message = models.ForeignKey(
        OutgoingEmailMessage,
        on_delete=models.CASCADE,
        related_name='attachments',
        null=True,
        blank=True
    )

    file = models.FileField(upload_to="unibox_chat_docs/attachments/")
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size = models.IntegerField(help_text="Size in bytes")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.filename} ({self.mime_type})"

    class Meta:
        verbose_name = "Attachment"


