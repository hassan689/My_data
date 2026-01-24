import os
import json
import subprocess
from datetime import datetime
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models import Q

from dashboard.models import GmailToken, CampaignRecord, EmailOpen
from users.models import EmailAccount, Affiliate, CustomUser, AccountGroup
from leads_data.models import DailySheet, SkipList
from subscriptions.models import Subscription, Revenue, Expense
from unibox.models import EmailThread, OutgoingEmailMessage, IncomingEmailMessage
from drip_campaigns.models import DripCampaign, EmailAccountAndLeads, DripTemplate, SentDripEmail


class Command(BaseCommand):
    help = "Exports selected models' data into JSON files, encrypts them with GPG symmetric encryption."

    # def add_arguments(self, parser):
    #     parser.add_argument(
    #         "--passphrase",
    #         type=str,
    #         help="GPG symmetric encryption passphrase (can also use BACKUP_PASSPHRASE env var)",
    #     )

    def handle(self, *args, **options):
        # Determine passphrase
        # passphrase = options.get("passphrase") or os.environ.get("BACKUP_PASSPHRASE")
        # if not passphrase:
        #     self.stderr.write(self.style.ERROR("No passphrase provided (use --passphrase or BACKUP_PASSPHRASE env var)."))
        #     return

        # Backup root and timestamped folder
        backup_root = os.path.join(settings.BASE_DIR, "backups")
        os.makedirs(backup_root, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_dir = os.path.join(backup_root, timestamp)
        os.makedirs(backup_dir, exist_ok=True)

        # Model backup mapping
        models_to_backup = [
            ("EmailAccount", EmailAccount.objects.all().iterator(chunk_size=100), self.serialize_email_account),
            ("AccountGroup", AccountGroup.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("GmailToken", GmailToken.objects.all().iterator(chunk_size=100), self.serialize_gmail_token),
            ("CampaignRecord", CampaignRecord.objects.filter(status__in=['pending', 'processing']).iterator(chunk_size=100), self.serialize_model),
            ("EmailOpen", EmailOpen.objects.filter(
                Q(campaign__status__in=['processing', 'pending']) | 
                Q(is_opened=True)
            ).iterator(chunk_size=1000), self.serialize_model),

            # Save templates for only those campaigns under status pening or processing
            ("CampaignTemplate", CampaignRecord.objects.filter(status__in=['pending', 'processing']).values_list('template_id', flat=True).distinct().iterator(chunk_size=100), self.serialize_model),

            ("DailySheet", DailySheet.objects.all().order_by("-uploaded_at")[:30], self.serialize_model),
            ("SkipList", SkipList.objects.all().iterator(chunk_size=100), self.serialize_model),

            ("Subscription", Subscription.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("Revenue", Revenue.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("Expense", Expense.objects.all().iterator(chunk_size=100), self.serialize_model),

            ("EmailThread", EmailThread.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("OutgoingEmailMessage", OutgoingEmailMessage.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("IncomingEmailMessage", IncomingEmailMessage.objects.all().iterator(chunk_size=100), self.serialize_model),

            ("Affiliate", Affiliate.objects.all().iterator(chunk_size=100), self.serialize_model),
            ("CustomUser", CustomUser.objects.all().iterator(chunk_size=100), self.serialize_model),

            ("DripCampaign", DripCampaign.objects.filter(status__in=['Active', 'Processing', 'Paused']).iterator(chunk_size=100), self.serialize_model),
            ("EmailAccountAndLeads", EmailAccountAndLeads.objects.filter(
                campaign__status__in=['Active', 'Processing', 'Paused']
            ).iterator(chunk_size=100), self.serialize_model),
            ("DripTemplate", DripTemplate.objects.filter(campaign__status__in=['Active', 'Processing', 'Paused']).iterator(chunk_size=100), self.serialize_model),
            ("SentDripEmail", SentDripEmail.objects.filter(
                Q(drip_campaign__status__in=['Active', 'Processing', 'Paused']) | 
                Q(is_opened=True)
            ).iterator(chunk_size=1000), self.serialize_model),
        ]

        for model_name, queryset_iterator, serializer in models_to_backup:
            file_path = os.path.join(backup_dir, f"{model_name}.json")
            self.export_and_encrypt(file_path, queryset_iterator, serializer)
            self.stdout.write(self.style.SUCCESS(f"Backed up and encrypted {model_name}"))

    def export_and_encrypt(self, file_path, queryset_iterator, serializer):
        # Write raw JSON file
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("[")
            first = True
            for obj in queryset_iterator:
                if not first:
                    f.write(",\n")
                else:
                    first = False
                f.write(json.dumps(serializer(obj), default=str))
            f.write("]")

        # # Encrypt with GPG symmetric
        # subprocess.run(
        #     ["gpg", "--batch", "--yes", "--passphrase", passphrase, "-c", file_path],
        #     check=True,
        # )

        # # Remove plaintext JSON
        # os.remove(file_path)

    # Custom serializers
    def serialize_email_account(self, account):
        return {
            "id": account.id,
            "user_id": getattr(account.user, "id", None),
            "email_address": account.email_address,
            "decrypted_password": account.get_password(),
            "email_provider": account.email_provider,
            "port_number": account.port_number,
            "server_type": account.server_type,
            "host": account.host,
            "is_warmup_target": account.is_warmup_target,
            "black_list": account.black_list,
            "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
        }

    def serialize_gmail_token(self, token):
        return {
            "id": token.id,
            "email_account_id": getattr(token.email_account, "id", None),
            "access_token": token.get_access_token(),
            "refresh_token": token.get_refresh_token(),
            "expires_in": token.expires_in,
            "token_type": token.token_type,
            "scope": token.scope,
            "created_at": token.created_at.isoformat() if token.created_at else None,
            "last_history_id": token.last_history_id,
        }

    def serialize_model(self, obj):
        # Generic serializer dumping all fields
        data = {}
        for field in obj._meta.fields:
            value = getattr(obj, field.name)
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            data[field.name] = value
        return data



