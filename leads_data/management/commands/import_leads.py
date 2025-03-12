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

        # Store existing MC Numbers to avoid duplicates
        existing_mc_numbers = set(Lead.objects.exclude(mc_number="").values_list("mc_number", flat=True))
        new_leads = []

        for _, row in tqdm(df.iterrows(), total=len(df), desc=f"Importing {DATA_FILE}"):
            try:
                new_leads.append(
                    Lead(
                        mc_number=row.get("MC Number", "").strip(),
                        status=row.get("USDOT Status", "").strip(),
                        legal_name=row.get("Legal Name", "").strip(),
                        telephone=row.get("Telephone", "").strip(),
                        email=row.get("Email", "").strip(),
                        address=row.get("Address", "").strip(),
                        usdot=row.get("U.S DOT", "").strip(),
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