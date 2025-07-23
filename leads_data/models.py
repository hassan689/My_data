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
    city = models.CharField(max_length=60, null=True, blank=True)
    state = models.CharField(max_length=20, null=True, blank=True)
    
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
        is_new_upload = self._state.adding
        super().save(*args, **kwargs)

        # Only calculate row count for new uploads or changed files
        if is_new_upload and self.file:
            try:
                # Load the Excel file into a DataFrame
                df = pd.read_excel(self.file)
                self.row_count = len(df)

                leads_to_create = []
                for index, row_data in df.iterrows():
                    # Ensure MC Number has "MC " prefix if not already present
                    mc_num = str(row_data.get("MC Number", "")).strip()
                    if mc_num and not mc_num.startswith("MC "):
                        mc_num = f"MC {mc_num}"

                    lead = Lead(
                        mc_number=mc_num,
                        status=str(row_data.get("USDOT Status", "")).strip(),
                        legal_name=str(row_data.get("Legal Name", "")).strip(),
                        telephone=str(row_data.get("Telephone", "")).strip(),
                        email=str(row_data.get("Email", "")).strip(),
                        address=str(row_data.get("Address", "")).strip(),
                        usdot=str(row_data.get("U.S DOT", "")).strip(),
                        vehicle_miles_traveled=str(row_data.get("Vehicle Miles Traveled", "")).strip(),
                        vmt_year=str(row_data.get("VMT Year", "")).strip(),
                        power_units=str(row_data.get("Power Units", "")).strip(),
                        duns_number=str(row_data.get("DUNS Number", "")).strip(),
                        drivers=str(row_data.get("Drivers", "")).strip(),
                        carrier_operation=str(row_data.get("Carrier Operation", "")).strip(),
                        passenger=str(row_data.get("Passenger", "")).strip(),
                        hm=str(row_data.get("HM", "")).strip(),
                        hhg=str(row_data.get("HHG", "")).strip(),
                        new_entrant=str(row_data.get("New Entrant", "")).strip(),
                        operation_classification=str(row_data.get("Operation Classification", "")).strip(),
                        cargo_classifications=str(row_data.get("Cargo Classifications", "")).strip(),
                        cargo_info=str(row_data.get("Cargo Info", "")).strip(),
                    )
                    leads_to_create.append(lead)

                Lead.objects.bulk_create(leads_to_create, ignore_conflicts=True)
                super().save(update_fields=['row_count'])

            except Exception as e:
                print(f"Error processing Daily Sheet file {self.file.name}: {e}")



