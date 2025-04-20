from django import forms
from users.models import EmailAccount
from django_ckeditor_5.widgets import CKEditor5Widget

class EmailAccountForm(forms.ModelForm):
    decrypted_password = forms.CharField(
        widget=forms.PasswordInput(render_value=True),
        label="Email Password",
        required=True
    )

    class Meta:
        model = EmailAccount
        fields = ["email_address", "decrypted_password", "email_provider", "port_number", "server_type", "host"]

    def __init__(self, *args, **kwargs):
        """Auto-fill decrypted password when editing an email account."""
        super().__init__(*args, **kwargs)
        if self.instance.id:  # If editing an existing account, pre-fill decrypted password
            try:
                self.fields["decrypted_password"].initial = self.instance.get_password()
            except Exception:
                self.fields["decrypted_password"].initial = ""

    def clean_decrypted_password(self):
        """Ensure decrypted password is required when creating a new entry."""
        password = self.cleaned_data.get("decrypted_password")

        if not self.instance.id and not password:  # New entry requires password
            raise forms.ValidationError("Password is required for new email accounts.")

        return password

    def save(self, commit=True):
        """Encrypt and save password securely."""
        email_account = super().save(commit=False)

        # Encrypt the password only if a new one was provided
        if self.cleaned_data.get("decrypted_password"):
            email_account.set_password(self.cleaned_data["decrypted_password"])

        if commit:
            email_account.save()
        return email_account


class CampaignForm(forms.Form):
    email_subject = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Some Big Offer - Hello [name] - [mc_number]'})
    )
    email_body = forms.CharField(
        widget=CKEditor5Widget(config_name='default'),  # Integrate CKEditor 5
        required=True
    )
    file_upload = forms.FileField(
        required=False, 
        widget=forms.ClearableFileInput(attrs={'class': 'hidden'})
    )
    mc_number = forms.CharField(
        max_length=10, 
        required=False, 
        widget=forms.TextInput(attrs={'placeholder': 'Enter Starting MC Number'})
    )
    targets_count = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={'placeholder': 'Number of targets you want to select'})
		)
    delay = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={'placeholder': '30 or 60 or xyz seconds / minutes'}),
        help_text='Enter a positive number for delay'
    )
    TIME_UNITS = [
        ('seconds', 'Seconds'),
        ('minutes', 'Minutes'),
    ]
    delay_unit = forms.ChoiceField(
        choices=TIME_UNITS,
        initial='seconds',
        widget=forms.Select(),
        help_text='Choose the time unit for delay'
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)  # Get user instance
        super().__init__(*args, **kwargs)

        if self.user and self.user.on_free_trial:
            self.fields['mc_number'].widget.attrs['disabled'] = True  # Restrict free trial users

    def clean(self):
        cleaned_data = super().clean()
        file_upload = cleaned_data.get('file_upload')
        mc_number = cleaned_data.get('mc_number')
        targets_count = cleaned_data.get('targets_count')
        delay = cleaned_data.get('delay')

        if self.user and self.user.on_free_trial:
            if not file_upload:
                self.add_error('file_upload', "Free trial users must upload an Excel file.")
        else:
            if not file_upload and not mc_number:
                raise forms.ValidationError("Either upload an Excel file or provide an MC number.")

        if targets_count is not None and targets_count < 1:
            self.add_error('targets_count', "Targets count cannot be less than 1.")

        if delay is not None and delay < 0:
            self.add_error('delay', "Delay must be 0 or a number greater than 0.")

        return cleaned_data

class BulkCampaignForm(CampaignForm):
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, user=self.user, **kwargs)

        # Set all fields as not required for Step 2
        for field_name in ['file_upload', 'mc_number', 'targets_count', 'email_subject', 'email_body', 'delay', 'delay_unit']:
            if field_name in self.fields:
                self.fields[field_name].required = False

    def clean(self):
        cleaned_data = self.cleaned_data  # get initial cleaned_data without triggering parent clean

        # Manual validation for delay field
        delay = cleaned_data.get('delay')
        if delay is not None and delay < 0:
            self.add_error('delay', "Delay must be 0 or a number greater than 0.")

        # Only validate account allocation if 'submit_allocation' is present in the data
        if 'submit_allocation' in self.data:
            selected_ids = self.data.getlist('selected_accounts')
            if not selected_ids:
                raise forms.ValidationError("Please select at least one email account.")

            for account_id in selected_ids:
                field_name = f'emails_for_account_{account_id}'
                value = self.data.get(field_name)
                if not value:
                    self.add_error(None, f"Missing email count for account ID {account_id}.")
                else:
                    try:
                        int_val = int(value)
                        if int_val < 1:
                            self.add_error(None, f"Email count must be at least 1 for account ID {account_id}.")
                    except ValueError:
                        self.add_error(None, f"Invalid number format for account ID {account_id}.")

        return cleaned_data
