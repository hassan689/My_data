from django.db import models
from django.core.mail import EmailMessage
from django.conf import settings
from users.models import CustomUser
from django.db.models import Q
from concurrent.futures import ThreadPoolExecutor
from datetime import date


# Should also have a date set to auto, cause we need to have the record of on what date was this mc number's data was added
class Lead(models.Model):
    
    mc_number = models.CharField(max_length=50, verbose_name="MC Number")  # Unique identifier
    status = models.CharField(max_length=150)
    legal_name = models.CharField(max_length=255)
    telephone = models.CharField(max_length=50, null=True, blank=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    usdot = models.CharField(max_length=50, verbose_name="USDOT")
    vehicle_miles_traveled = models.CharField(max_length=150, null=True, blank=True)
    vmt_year = models.CharField(max_length=50, null=True, blank=True)
    power_units = models.CharField(max_length=50, null=True, blank=True)
    duns_number = models.CharField(max_length=50, null=True, blank=True)
    drivers = models.CharField(max_length=50, null=True, blank=True)
    carrier_operation = models.CharField(max_length=255)
    passenger = models.CharField(max_length=10, null=True, blank=True)  # True/False stored as text
    hm = models.CharField(max_length=10, null=True, blank=True, verbose_name="HM")  # Hazardous Materials
    hhg = models.CharField(max_length=10, null=True, blank=True, verbose_name="HHG")  # Household Goods
    new_entrant = models.CharField(max_length=10, null=True, blank=True)
    operation_classification = models.TextField(null=True, blank=True)
    cargo_classifications = models.TextField(null=True, blank=True)
    cargo_info = models.TextField(null=True, blank=True)
    
    added_on = models.DateField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.mc_number} - {self.legal_name}"

    class Meta:
        verbose_name = "Carrier Data"
        verbose_name_plural = "Carrier Leads"
        


class DailySheet(models.Model):
    file = models.FileField(upload_to="daily_sheets/")  # Saves files in 'media/daily_sheets/'
    uploaded_at = models.DateTimeField(auto_now_add=True)  # Stores the timestamp when the file was uploaded

    def __str__(self):
        return f"Daily Sheet - {self.uploaded_at.strftime('%Y-%m-%d %H:%M:%S')}"
    
    def save(self, *args, **kwargs):
        # Save first to ensure file is accessible
        super().save(*args, **kwargs)

        # Get relevant users
        users = CustomUser.objects.filter(
            Q(on_free_trial=True) | Q(subscription__status="active")
        )
        recipient_list = list(users.values_list("email", flat=True))

        if recipient_list:
            today_str = date.today().strftime("%B %d, %Y")  # Example: April 10, 2025
            subject = f"New daily sheet from FMCSA for {today_str}"
            body = "Please find the attached daily sheet."
            from_email = settings.EMAIL_HOST_USER
            file_path = self.file.path

            # Send email in background
            executor = ThreadPoolExecutor(max_workers=5)
            executor.submit(self.send_email_with_file, subject, body, from_email, recipient_list, file_path)

    def send_email_with_file(self, subject, body, from_email, to_list, file_path):
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=from_email,
            to=to_list,
        )
        email.attach_file(file_path)
        email.send()

