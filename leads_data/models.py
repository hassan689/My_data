from django.db import models


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

    def __str__(self):
        return f"{self.mc_number} - {self.legal_name}"

    class Meta:
        verbose_name = "Carrier Data"
        verbose_name_plural = "Carrier Leads"