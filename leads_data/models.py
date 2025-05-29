from django.db import models
import pandas as pd


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
    row_count = models.PositiveIntegerField(default=0, editable=False)

    def __str__(self):
        return f"Daily Sheet - {self.uploaded_at.strftime('%Y-%m-%d %H:%M:%S')}"

    def save(self, *args, **kwargs):
        # Only calculate row count for new uploads or changed files
        if self.file:
            try:
                # Load the Excel file into a DataFrame
                df = pd.read_excel(self.file)
                self.row_count = len(df)
            except Exception as e:
                self.row_count = 0  # fallback if the file is not readable

        super().save(*args, **kwargs)


