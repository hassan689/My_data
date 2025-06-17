from django.db import models
from users.models import EmailAccount


class GmailToken(models.Model):
    email_account = models.OneToOneField(EmailAccount, on_delete=models.CASCADE, related_name="gmail_token", null=True, blank=True) #remove null and blank afterwards
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_in = models.IntegerField()
    token_type = models.CharField(max_length=50)
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email_account} - {self.created_at}"



