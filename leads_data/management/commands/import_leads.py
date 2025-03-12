import os
import pandas as pd
from django.core.management.base import BaseCommand
from leads_data.models import Lead
from tqdm import tqdm

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../data/complete_data.xlsx")

class Command(BaseCommand):
    help = "Import leads from a single Excel file using bulk create"

    def handle(self, *args, **kwargs):
        """Import leads from complete_data.xlsx"""

        if not os.path.exists(DATA_FILE):
            self.stderr.write(self.style.ERROR(f"❌ File not found: {DATA_FILE}"))
            return

        self.stdout.write(self.style.NOTICE(f"📂 Processing file: complete_data.xlsx..."))

        try:
            df = pd.read_excel(DATA_FILE, dtype=str).fillna("")
            print("🔍 Preview of DataFrame:")
            print(df.head(), flush=True)  # Force immediate output
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Error reading file: {e}"))
            return

        # Function to clean integer values
        def clean_int(value):
            if isinstance(value, str):
                value = value.replace(",", "").strip()  # Remove commas
            try:
                return int(value) if value.isdigit() else None
            except ValueError:
                return None  # Skip invalid values safely

        def clean_bool(value):
            return str(value).strip().lower() in ["true", "1", "yes"]

        # Store existing MCNumbers, ignoring empty ones
        existing_mc_numbers = set(Lead.objects.exclude(mc_number="").values_list("mc_number", flat=True))
        new_leads = []

        for _, row in tqdm(df.iterrows(), total=len(df), desc=f"Importing {DATA_FILE}"):
            try:
                # Remove commas and convert MC Number to int safely
                mc_number = row["MCNumber"]
                if pd.notna(mc_number):
                    mc_number = int(mc_number.replace(",", ""))  # Remove commas

                # Avoid duplicate entries
                if mc_number in existing_mc_numbers:
                    self.stdout.write(f"Skipping duplicate MCNumber: {mc_number}")
                    continue

                new_leads.append(
                    Lead(
                        mc_number=mc_number,
                        legal_name=row.get("LegalName", "").strip(),
                        telephone=row.get("Telephone", "").strip(),
                        email=row.get("Email", "").strip(),
                        address=row.get("Address", "").strip(),
                        usdot=row.get("USDOT", "").strip(),
                        vehicle_miles_traveled=row.get("Vehicle Miles Traveled", "").strip(),
                        vmt_year=row.get("VMT Year", "").strip(),
                        power_units=row.get("Power Units", "").strip(),
                        duns_number=row.get("DUNS Number", "").strip(),
                        drivers=row.get("Drivers", "").strip(),
                        carrier_operation=row.get("Carrier Operation", "").strip(),
                        passenger=row.get("Passenger", "").strip(),
                        hm=row.get("HM", "").strip(),
                        hhg=row.get("HHG", "").strip(),
                        new_entrant=row.get("New Entrant", "").strip(),
                        operation_classification=row.get("Operation Classification", "").strip(),
                        cargo_classifications=row.get("Cargo Classifications", "").strip(),
                        cargo_info=row.get("Cargo Info", "").strip(),
                    )
                )
            except ValueError as e:
                self.stderr.write(f"Skipping row due to error: {e}")
            except Exception as e:
                self.stderr.write(f"Unexpected error: {e}")

        # Bulk insert to improve performance
        if new_leads:
            Lead.objects.bulk_create(new_leads, ignore_conflicts=True)
            self.stdout.write(self.style.SUCCESS(f"✅ Successfully imported {len(new_leads)} leads."))
