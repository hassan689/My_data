from django.core.management.base import BaseCommand
from users.models import EmailProvider, EmailAccount  # Ensure this matches your app name
from django.db.models import Q

class Command(BaseCommand):
    help = 'Seeds the database with standard SSL/465 email provider settings'

    def standardize_email_accounts(self):
        # 1. Get all standardized templates from your new table
        templates = EmailProvider.objects.all()
        
        # 2. Define the fuzzy mapping for known typos found in your DB
        # This maps common user typos to the 'mx_keyword' in your EmailProvider table
        typo_map = {
            'googlw': 'google.com',
            'goolgle': 'google.com',
            'google.com': 'google.com',
            'google workspace': 'google.com',
            'google yahoo': 'google.com', # Assuming primary is Google
            'hoatgator': 'hostinger.com', # Mapping similar host names if applicable
            'hostgator': 'hostinger.com',
            'hostingwr': 'hostinger.com',
            'microsoft': 'outlook.com',
            'microsoft 365': 'outlook.com',
            'private email': 'registrar-servers.com',
            'name cheap': 'registrar-servers.com',
        }

        print("--- Starting Data Cleanup ---")
        
        accounts = EmailAccount.objects.all()
        updated_count = 0

        for account in accounts:
            provider_str = (account.email_provider or "").lower().strip()
            matched_provider = None

            # Logic A: Check our explicit typo map first
            if provider_str in typo_map:
                keyword = typo_map[provider_str]
                matched_provider = EmailProvider.objects.filter(mx_keyword=keyword).first()

            # Logic B: If no typo match, try a direct keyword search in the EmailProvider table
            if not matched_provider:
                # Look for a provider whose name or keyword is inside the user's string
                matched_provider = EmailProvider.objects.filter(
                    Q(name__icontains=provider_str) | Q(mx_keyword__icontains=provider_str)
                ).first()

            # 3. Apply the "Gold Standard" settings
            if matched_provider:
                print(f"Fixing [{account.email_address}]: '{account.email_provider}' -> '{matched_provider.name}'")
                
                account.email_provider = matched_provider.name
                account.host = matched_provider.smtp_host
                account.port_number = matched_provider.smtp_port
                account.imap_host = matched_provider.imap_host
                account.imap_port = matched_provider.imap_port
                account.server_type = matched_provider.server_type
                account.save()
                updated_count += 1
            else:
                print(f"⚠️ Could not auto-fix [{account.email_address}]: '{provider_str}'. Manual update required.")

        print(f"--- Cleanup Complete. Updated {updated_count} accounts. ---")
    

    def handle(self, *args, **kwargs):
        providers = [
            {
                'name': 'Gmail / Google Workspace',
                'mx_keyword': 'google.com',
                'smtp_host': 'smtp.gmail.com',
                'smtp_port': 465,
                'imap_host': 'imap.gmail.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Hostinger',
                'mx_keyword': 'hostinger.com',
                'smtp_host': 'smtp.hostinger.com',
                'smtp_port': 465,
                'imap_host': 'imap.hostinger.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Namecheap / PrivateEmail',
                'mx_keyword': 'registrar-servers.com',
                'smtp_host': 'mail.privateemail.com',
                'smtp_port': 465,
                'imap_host': 'mail.privateemail.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'GoDaddy',
                'mx_keyword': 'secureserver.net',
                'smtp_host': 'smtpout.secureserver.net',
                'smtp_port': 465,
                'imap_host': 'imap.secureserver.net',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Titan',
                'mx_keyword': 'titan.email',
                'smtp_host': 'smtp.titan.email',
                'smtp_port': 465,
                'imap_host': 'imap.titan.email',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Zoho (Free/Personal)',
                'mx_keyword': 'zoho.com',
                'smtp_host': 'smtp.zoho.com',
                'smtp_port': 465,
                'imap_host': 'imap.zoho.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Zoho Pro (Paid/Org)',
                'mx_keyword': 'zoho.com',
                'smtp_host': 'smtppro.zoho.com',
                'smtp_port': 465,
                'imap_host': 'imappro.zoho.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
            {
                'name': 'Yahoo',
                'mx_keyword': 'yahoo.com',
                'smtp_host': 'smtp.mail.yahoo.com',
                'smtp_port': 465,
                'imap_host': 'imap.mail.yahoo.com',
                'imap_port': 993,
                'server_type': 'SSL'
            },
        ]

        for p_data in providers:
            # We use name as the unique identifier for seeding
            obj, created = EmailProvider.objects.update_or_create(
                name=p_data['name'],
                defaults=p_data
            )
            status = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{status} {p_data['name']}"))

        # After seeding the providers, we can standardize existing accounts
        self.standardize_email_accounts()

