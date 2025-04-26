from django import forms
from users.models import IMAPSettings

class IMAPSettingsForm(forms.ModelForm):
    class Meta:
        model = IMAPSettings
        fields = ['imap_host', 'imap_port', 'imap_encryption']
        widgets = {
            'imap_encryption': forms.Select(choices=IMAPSettings._meta.get_field('imap_encryption').choices),
        }
