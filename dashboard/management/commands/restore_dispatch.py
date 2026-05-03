import json
import os
from django.core.management.base import BaseCommand
from django.apps import apps
from django.db import transaction, IntegrityError

class Command(BaseCommand):
    help = "High-performance restore that handles orphaned Foreign Keys."

    def add_arguments(self, parser):
        parser.add_argument('--dir', type=str, required=True)

    def handle(self, *args, **options):
        data_dir = options['dir']
        batch_size = 500
        
        # Dependency order remains the same
        import_map = [
            ('users', 'CustomUser', 'CustomUser.json'),
            ('users', 'Affiliate', 'Affiliate.json'),
            ('users', 'AccountGroup', 'AccountGroup.json'),
            ('users', 'EmailAccount', 'EmailAccount.json'),
            ('dashboard', 'GmailToken', 'GmailToken.json'),
            ('dashboard', 'CampaignTemplate', 'CampaignTemplate.json'),
            ('dashboard', 'CampaignRecord', 'CampaignRecord.json'),
            ('dashboard', 'EmailOpen', 'EmailOpen.json'),
            ('drip_campaigns', 'DripCampaign', 'DripCampaign.json'),
            ('drip_campaigns', 'DripTemplate', 'DripTemplate.json'),
            ('drip_campaigns', 'DripVariation', 'DripVariation.json'),
            ('drip_campaigns', 'EmailAccountAndLeads', 'EmailAccountAndLeads.json'),
            ('drip_campaigns', 'SentDripEmail', 'SentDripEmail.json'),
            ('unibox', 'EmailThread', 'EmailThread.json'),
            ('unibox', 'OutgoingEmailMessage', 'OutgoingEmailMessage.json'),
            ('unibox', 'IncomingEmailMessage', 'IncomingEmailMessage.json'),
            ('subscriptions', 'Subscription', 'Subscription.json'),
            ('subscriptions', 'Revenue', 'Revenue.json'),
            ('subscriptions', 'Expense', 'Expense.json'),
            ('leads_data', 'DailySheet', 'DailySheet.json'),
            ('leads_data', 'SkipList', 'SkipList.json'),
            ('dashboard', 'VerificationBatch', 'VerificationBatch.json'),
        ]

        for app_label, model_name, file_name in import_map:
            file_path = os.path.join(data_dir, file_name)
            if not os.path.exists(file_path):
                continue

            Model = apps.get_model(app_label, model_name)
            with open(file_path, 'r', encoding='utf-8') as f:
                entries = json.load(f)

            self.stdout.write(f"[*] Processing {model_name}...")
            fk_fields = {f.name for f in Model._meta.fields if f.is_relation and not f.many_to_many}

            for i in range(0, len(entries), batch_size):
                batch = entries[i:i + batch_size]
                
                # Use a narrower transaction to handle individual batch failures
                try:
                    with transaction.atomic():
                        to_create = []
                        m2m_tasks = []

                        for entry in batch:
                            pk = entry.pop('id', entry.pop('pk', None))
                            
                            # Capture encryption fields[cite: 2, 3]
                            decrypted_pass = entry.pop('decrypted_password', None)
                            raw_access = entry.pop('access_token', None)
                            raw_refresh = entry.pop('refresh_token', None)

                            # Handle M2M
                            m2m_entry_data = {f.name: entry.pop(f.name) for f in Model._meta.many_to_many if f.name in entry}

                            # Nullify User referrals for first pass[cite: 2]
                            if model_name == 'CustomUser' and 'referred_by' in entry:
                                entry.pop('referred_by')

                            # Remap FKs to _id[cite: 3]
                            final_entry = { (f"{k}_id" if k in fk_fields and not k.endswith('_id') else k): v for k, v in entry.items() }

                            obj = Model(id=pk, **final_entry)

                            # Encryption setters[cite: 3]
                            if decrypted_pass and hasattr(obj, 'set_password'): obj.set_password(decrypted_pass)
                            if raw_access and hasattr(obj, 'set_access_token'): obj.set_access_token(raw_access)
                            if raw_refresh and hasattr(obj, 'set_refresh_token'): obj.set_refresh_token(raw_refresh)

                            to_create.append(obj)
                            if m2m_entry_data: m2m_tasks.append((obj, m2m_entry_data))

                        # Attempt bulk creation[cite: 2]
                        Model.objects.bulk_create(to_create, ignore_conflicts=True)

                        for obj, m2m_data in m2m_tasks:
                            for field_name, pks in m2m_data.items():
                                if pks: getattr(obj, field_name).set(pks)

                except IntegrityError:
                    # If the batch fails due to a missing FK (like ID 3102), 
                    # we fall back to a slower one-by-one save for THIS batch only.
                    for obj in to_create:
                        try:
                            with transaction.atomic():
                                obj.save()
                        except IntegrityError:
                            # Skip orphaned records[cite: 3]
                            continue

            self.stdout.write(self.style.SUCCESS(f"[+] Finished {model_name}"))

        # Final Pass: Restore User Referrals[cite: 2]
        self.stdout.write("[*] Final Pass: Restoring User Referral relationships...")
        user_file = os.path.join(data_dir, "CustomUser.json")
        if os.path.exists(user_file):
            with open(user_file, 'r', encoding='utf-8') as f:
                users = json.load(f)
            
            CustomUser = apps.get_model('users', 'CustomUser')
            updates = []
            for u_data in users:
                ref_id = u_data.get('referred_by')
                if ref_id:
                    updates.append(CustomUser(id=u_data.get('id'), referred_by_id=ref_id))
            
            if updates:
                CustomUser.objects.bulk_update(updates, ['referred_by_id'], batch_size=batch_size)

        self.stdout.write(self.style.SUCCESS("[+] RESTORE COMPLETE"))