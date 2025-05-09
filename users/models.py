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



class CustomUser(AbstractUser):
    phone_number = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=255)
    website_link = models.URLField(null=True, blank=True)
    mc_number = models.CharField(max_length=50, null=True, blank=True, verbose_name="MC Number")

    on_free_trial = models.BooleanField(default=True, verbose_name="On Free Trial")

    groups = models.ManyToManyField("auth.Group", related_name="customuser_set", blank=True)
    user_permissions = models.ManyToManyField("auth.Permission", related_name="customuser_set", blank=True)

    def has_active_subscription(self):
        return self.subscriptions.filter(status="active").exists()
        
    def is_free_trial_expired(self):
        """Check if 7 days have passed since user creation."""
        return now() >= self.date_joined + timedelta(days=7)

    def __str__(self):
        return self.username



class EmailAccount(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="email_accounts")
    email_address = models.EmailField(unique=True)  # Unique globally
    encrypted_password = models.TextField(verbose_name="Email Password")  # Store encrypted passwords securely
    last_used_at = models.DateTimeField(null=True, blank=True)  # Track last usage
    
    email_provider = models.CharField(max_length=100, verbose_name="Email Provider")  # Example: Gmail, Outlook, Yahoo
    port_number = models.IntegerField(verbose_name="Port Number")  # SMTP/IMAP Port Number
    server_type = models.CharField(
        max_length=10,
        choices=[("TLS", "TLS"), ("SSL", "SSL")],
        verbose_name="SMTP Server Type",
    )
    host = models.CharField(max_length=100, verbose_name="Outgoing Servr Host")

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
        # Ensure user is assigned before validation
        if not self.user:
            raise ValidationError("User must be assigned before saving.")

        is_new = self.id is None  # This tells us if it's a new object
        if is_new:
            email_account_count = self.user.email_accounts.count()

            if self.user.on_free_trial and email_account_count >= 3:
                raise ValidationError("Cannot add more than 3 email accounts on free trial.")

        # Proceed with saving
        super().save(*args, **kwargs)


    def __str__(self):
        return f"{self.user.username} - {self.email_address}"

    class Meta:
        verbose_name = "Email Account"
        verbose_name_plural = "Email Accounts"


# Synchronizing it with the email account model on creation and deletion
class IMAPSettings(models.Model):
    email_account = models.OneToOneField(EmailAccount, on_delete=models.CASCADE, related_name="imap_settings")
    
    imap_host = models.CharField(max_length=100, verbose_name="IMAP Server Host")  # e.g. imap.gmail.com
    imap_port = models.IntegerField(verbose_name="IMAP Port Number")  # IMAP Port (usually 993 for SSL)
    imap_encryption = models.CharField(
        max_length=10,
        choices=[("SSL", "SSL"), ("TLS", "TLS")],
        verbose_name="IMAP Encryption"
    )
    
    def __str__(self):
        return f"IMAP Settings for {self.email_account.email_address}"

    class Meta:
        verbose_name = "IMAP Settings"
        verbose_name_plural = "IMAP Settings"

