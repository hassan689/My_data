from django.contrib.auth.models import AbstractUser
from django.db import models
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ValidationError
import base64
from django.utils.timezone import now, timedelta



def get_cipher():
    """Ensure ENCRYPT_KEY is properly converted to bytes before using it."""
    if isinstance(settings.ENCRYPT_KEY, str):  
        key = base64.urlsafe_b64decode(settings.ENCRYPT_KEY)  # Decode base64 string
    else:
        key = settings.ENCRYPT_KEY  # Already in bytes
    return Fernet(key)

def encrypt_password(password: str) -> str:
    """Encrypts the password using Fernet encryption."""
    cipher = get_cipher()
    return cipher.encrypt(password.encode()).decode()  # Encrypt and return as string

def decrypt_password(encrypted_password: str) -> str:
    """Decrypts the password using Fernet encryption."""
    cipher = get_cipher()
    return cipher.decrypt(encrypted_password.encode()).decode()  # Decrypt and return as string



# set the status to free trial on creation, then auto check it to false once 7 days are over and also send an email to the user to pay
# for the subscription ...... DONE
class CustomUser(AbstractUser):
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    company_name = models.CharField(max_length=255, null=True, blank=True)
    website_link = models.URLField(null=True, blank=True)
    mc_number = models.CharField(max_length=50, null=True, blank=True, verbose_name="MC Number")

    on_free_trial = models.BooleanField(default=True, verbose_name="On Free Trial")
    lifetime_value = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, null=True, blank=True) # Later remove this field on db change
    months_subscribed = models.IntegerField(default=0)

    groups = models.ManyToManyField("auth.Group", related_name="customuser_set", blank=True)
    user_permissions = models.ManyToManyField("auth.Permission", related_name="customuser_set", blank=True)

    def has_active_subscription(self):
        return self.subscriptions.filter(status="active").exists()

    def update_lifetime_value(self):
        if hasattr(self, "subscription"):  # Ensure the user has a subscription record
            self.months_subscribed = self.subscription.renewal_count  # Track renewals instead of number of subscriptions
            self.save(update_fields=["months_subscribed"])
        
    def is_free_trial_expired(self):
        """Check if 7 days have passed since user creation."""
        return now() >= self.date_joined + timedelta(days=7)

    def __str__(self):
        return self.username



# Need to record the port number and email provider name .... DONE
class EmailAccount(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="email_accounts")
    email_address = models.EmailField(unique=True)  # Unique globally
    encrypted_password = models.TextField(verbose_name="Email Password")  # Store encrypted passwords securely
    is_active = models.BooleanField(default=True)  # Need to remove it
    last_used_at = models.DateTimeField(null=True, blank=True)  # Track last usage
    
    total_campaigns = models.PositiveIntegerField(null=True, blank=True)
    email_provider = models.CharField(max_length=100, verbose_name="Email Provider", null=True, blank=True)  # Example: Gmail, Outlook, Yahoo
    port_number = models.IntegerField(verbose_name="Port Number", null=True, blank=True)  # SMTP/IMAP Port Number
    server_type = models.CharField(
        max_length=10,
        choices=[("TLS", "TLS"), ("SSL", "SSL")],
        verbose_name="SMTP Server Type",
        null=True, blank=True
    )
    host = models.CharField(max_length=100, verbose_name="Outgoing Servr Host", null=True, blank=True)

    def set_password(self, raw_password):
        """Encrypt and set the password securely."""
        if raw_password:
            self.encrypted_password = encrypt_password(raw_password)


    def get_password(self):
        """Decrypt and return the original password, or return an error message."""
        try:
            return decrypt_password(self.encrypted_password)
        except Exception:
            return None  # Return None instead of raising an error

    def check_password(self, raw_password):
        """Verify if the provided password matches the stored encrypted password."""
        return self.get_password() == raw_password  # Direct string comparison

    def save(self, *args, **kwargs):
        """Prevent more than 20 email accounts per user."""
        if self.user.email_accounts.count() >= 20:
            raise ValidationError("Cannot add more than 20 email accounts.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.email_address}"

    class Meta:
        verbose_name = "Email Account"
        verbose_name_plural = "Email Accounts"

