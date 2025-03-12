from django.db import models

class Lead(models.Model):
    
    mc_number = models.CharField(max_length=50, unique=True, verbose_name="MC Number")  # Unique identifier
    status = models.CharField(max_length=50)
    legal_name = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    usdot = models.CharField(max_length=50, unique=True, verbose_name="USDOT")
    vehicle_miles_traveled = models.BigIntegerField(null=True, blank=True)
    vmt_year = models.IntegerField(null=True, blank=True)
    power_units = models.IntegerField(null=True, blank=True)
    duns_number = models.CharField(max_length=50, null=True, blank=True)
    drivers = models.IntegerField(null=True, blank=True)
    carrier_operation = models.CharField(max_length=50)
    passenger = models.BooleanField(default=False)
    hm = models.BooleanField(default=False, verbose_name="HM")  # Hazardous Materials
    hhg = models.BooleanField(default=False, verbose_name="HHG")  # Household Goods
    new_entrant = models.BooleanField(default=False)
    operation_classification = models.CharField(max_length=255)
    cargo_classifications = models.CharField(max_length=255)
    cargo_info = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.mc_number} - {self.legal_name}"

    class Meta:
        verbose_name = ("Carrier Data")
        verbose_name_plural = ("Carrier Leads")

