from django.db import models
from users.models import EmailAccount

class EmailThread(models.Model):
    email_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name="threads")
    subject = models.CharField(max_length=255)
    started_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Thread: {self.subject} - ({self.email_account.email_address})"

