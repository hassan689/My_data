from django.contrib.auth.models import AbstractUser
from django.db import models
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ValidationError
import base64
from django.utils.timezone import now, timedelta
from decimal import Decimal



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


# def encrypt_otp(otp_code: str) -> str:
#     cipher = get_cipher()
#     return cipher.encrypt(otp_code.encode()).decode()

# def decrypt_otp(encrypted_code: str) -> str:
#     cipher = get_cipher()
#     return cipher.decrypt(encrypted_code.encode()).decode()




class CustomUser(AbstractUser):
    phone_number = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=255)
    website_link = models.URLField(null=True, blank=True)
    mc_number = models.CharField(max_length=50, null=True, blank=True, verbose_name="MC Number")
    on_free_trial = models.BooleanField(default=False, verbose_name="On Free Trial")

    # Field to track which affiliate referred this user
    # A user can be referred by an affiliate, or not (null=True, blank=True)
    referred_by = models.ForeignKey(
        'users.Affiliate', on_delete=models.SET_NULL, # If an affiliate is deleted, referred users remain, but their 'referred_by' becomes NULL
        null=True, blank=True, related_name="referred_users",
    )

    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_usage_count = models.PositiveIntegerField(default=0) # Track MC checks for the desktop scraper app during the trial

    # This stores the unique ID of the currently allowed device for the scraper desktop app
    desktop_session_id = models.CharField(max_length=100, null=True, blank=True)

    groups = models.ManyToManyField("auth.Group", related_name="customuser_set", blank=True)
    user_permissions = models.ManyToManyField("auth.Permission", related_name="customuser_set", blank=True)

    tracking_custom_domain = models.CharField( # their primary one
        max_length=255, 
        unique=True, 
        null=True, 
        blank=True, 
        help_text="Subdomain for email tracking (e.g., track.theircompany.com)"
    )
    tracking_domain_verified = models.BooleanField(
        default=False,
    )

    class Meta:
        # This creates the 'cheat sheet' for the database
        indexes = [
            models.Index(fields=['on_free_trial', 'trial_started_at']),
        ]
        verbose_name = "User"
        verbose_name_plural = "Users"

    def save(self, *args, **kwargs):
        
        if self.id:  # If this is an update, not a new creation
            old_instance = CustomUser.objects.get(id=self.id)
            # If it WAS False and is NOW True
            if not old_instance.on_free_trial and self.on_free_trial:
                self.trial_started_at = now()

            # Logic 2: Reset verification if domain changes
            # If the domain string has changed, we must force re-verification
            if old_instance.tracking_custom_domain != self.tracking_custom_domain:
                self.tracking_domain_verified = False

        elif self.on_free_trial: # If creating a new user with trial active
            self.trial_started_at = now()
            
        super().save(*args, **kwargs)

    def can_use_scraper(self):
        if self.is_superuser or self.has_active_subscription():
            return True
        if self.on_free_trial:
            return self.trial_usage_count <= 1000
        return False

    def has_active_subscription(self):
        """
        Checks if the user has a valid subscription.
        Updated to support OneToOneField relationship.
        """

        # 2. Check relationship exists
        if not hasattr(self, 'subscription'):
            return False
            
        # 3. Check status
        return self.subscription.status == "active"

        
    def is_free_trial_expired(self):
        
        """Check if 10 days have passed since the trial actually started."""
        # Use the new field if it exists, otherwise fallback to date_joined
        start_date = self.trial_started_at or self.date_joined
        return now() >= start_date + timedelta(days=10)

    def __str__(self):
        return self.username



class Affiliate(models.Model):
    
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="affiliate_profile")
    name = models.CharField(max_length=100, null=True, blank=True)
    joining_date = models.DateTimeField(auto_now_add=True)
    referral_code = models.CharField(max_length=50, unique=True,)
    is_active = models.BooleanField(default=True)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('20'))
    lifetime_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    has_been_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.name}"

    @property
    def pending_amount(self):
        return (self.lifetime_earnings - self.has_been_paid).quantize(Decimal('0.01'))



class AccountGroup(models.Model):
    
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="account_groups")
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'name')

    def __str__(self):
        return self.name


class EmailAccount(models.Model):

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="email_accounts")
    account_group = models.ForeignKey(AccountGroup, on_delete=models.SET_NULL, related_name="email_accounts", null=True, blank=True)

    email_address = models.EmailField(unique=True)  # Unique globally
    encrypted_password = models.TextField(verbose_name="Email Password")  # Store encrypted passwords securely
    last_used_at = models.DateTimeField(null=True, blank=True)  # Track last usage
    
    email_provider = models.CharField(max_length=100, verbose_name="Email Provider")  # Example: Gmail, Outlook, Yahoo
    port_number = models.IntegerField(verbose_name="Port Number")  # SMTP/IMAP Port Number
    server_type = models.CharField(
        max_length=10,
        choices=[("TLS", "TLS"), ("SSL", "SSL"), ("STARTTLS", "STARTTLS"),],
        verbose_name="SMTP Server Type",
    )
    host = models.CharField(max_length=100, verbose_name="Outgoing Servr Host")

    display_name = models.CharField(
        max_length=255, 
        null=True, 
        blank=True, 
        help_text="The name that appears in the recipient's inbox."
    )

    is_warmup_target = models.BooleanField(default=False)
    black_list = models.BooleanField(default=False) # These are for the accoutns that are causing trouble for the warmup

    tracking_custom_domain = models.CharField( # for each account
        max_length=255, 
        unique=True, 
        null=True, 
        blank=True, 
        help_text="Subdomain for email tracking (e.g., track.theircompany.com)"
    )
    
    '''
    This verfirication is for: 
    1. if this domain is actually from my DB/System and not outsider
    2. if it actually exists on the internet
    3. it points to my server"
    '''
    tracking_domain_verified = models.BooleanField(
        default=False,
    )

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
    
    @property
    def effective_tracking_domain(self):
        """
        Returns the domain to be used for tracking links.
        Priority:
        1. Account-specific domain (if verified)
        2. User-profile domain (if verified)
        """
        # 1. Check Account Specific
        if self.tracking_custom_domain and self.tracking_domain_verified:
            return self.tracking_custom_domain
        
        # 2. Check User Profile Fallback
        if self.user.tracking_custom_domain and self.user.tracking_domain_verified:
            return self.user.tracking_custom_domain
        
        return None  # Return Nothing if no valid domain is found


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
        return f"{self.email_address}"

    class Meta:
        verbose_name = "Email Account"
        verbose_name_plural = "Email Accounts"



# Leaving it alone here for now as migrations have been run with this model. Not required now as. Business decision.

class OTP(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='otps')
    encrypted_code = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    # def set_code(self, raw_code: str):
    #     self.encrypted_code = encrypt_otp(raw_code)

    # def check_code(self, raw_code: str) -> bool:
    #     try:
    #         return decrypt_otp(self.encrypted_code) == raw_code
    #     except Exception:
    #         return False

    def is_valid(self):
        return (now() - self.created_at).total_seconds() < 300 and not self.is_used
