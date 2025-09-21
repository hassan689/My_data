import json
from django.core.management.base import BaseCommand
from dashboard.models import GmailToken
from users.models import EmailAccount


class Command(BaseCommand):
    help = 'Exports decrypted data for EmailAccount and GmailToken models to a JSON file.'

    def handle(self, *args, **options):
        # Data for EmailAccount
        email_accounts_data = []
        for account in EmailAccount.objects.all():
            email_accounts_data.append({
                'id': account.id,
                'user_id': account.user.id,
                'email_address': account.email_address,
                'decrypted_password': account.get_password(),
                'email_provider': account.email_provider,
                'port_number': account.port_number,
                'server_type': account.server_type,
                'host': account.host,
                'is_warmup_target': account.is_warmup_target,
                'black_list': account.black_list,
            })
        
        # Data for GmailToken
        gmail_tokens_data = []
        for token in GmailToken.objects.all():
            gmail_tokens_data.append({
                'id': token.id,
                'email_account_id': token.email_account.id if token.email_account else None,
                'access_token': token.get_access_token(),
                'refresh_token': token.get_refresh_token(),
                'expires_in': token.expires_in,
                'token_type': token.token_type,
                'scope': token.scope,
                'created_at': token.created_at.isoformat(),
                'last_history_id': token.last_history_id,
            })
        
        # Combine and export to a single file
        combined_data = {
            'email_accounts': email_accounts_data,
            'gmail_tokens': gmail_tokens_data,
        }

        with open('decrypted_sensitive_data.json', 'w') as f:
            json.dump(combined_data, f, indent=4)
        
        self.stdout.write(self.style.SUCCESS('Successfully exported decrypted data to decrypted_sensitive_data.json'))


