from django.db import models
from django_mailbox.models import Mailbox
from itertools import chain

class EmailThread(models.Model):
    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE)  # Owner
    email1 = models.EmailField()  # sender role
    email2 = models.EmailField()  # recipient role, which is mostly my mailbox's address
    subject = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    # email1 and email2 fields are only there to keep track of the participants in the conversation not the sender/reciver logics .....
    # incoming and outgoing message instances have the correct record of the sender and the recipient

    def __str__(self):
        return f"{self.email2} -> {self.subject}" # email 2 is supposed to be the owner of the thread, since its on the receiving side for the inboxes

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


