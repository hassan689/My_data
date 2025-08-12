from django import forms
from django.utils import timezone
from users.models import EmailAccount
from django_ckeditor_5.widgets import CKEditor5Widget
from django.forms.widgets import DateTimeInput
from datetime import timezone as dt_timezone
import pytz
# from ckeditor_uploader.widgets import CKEditorUploadingWidget 

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

class DateTimePickerInput(DateTimeInput):
    input_type = 'datetime-local'

class CampaignForm(forms.Form):
    email_subject = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Hello [Legal Name] - [MC Number] - Some Big Offer'})
    )
    email_body = forms.CharField(
        widget=CKEditor5Widget(config_name='default'),
        required=True
    )
    file_upload = forms.FileField(
        required=False,
        widget=forms.ClearableFileInput(attrs={'class': 'hidden'})
    )
    lower_limit_mc_number = forms.CharField(
        max_length=10,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Lower Limit MC Number, e.g: 1600000'})
    )
    upper_limit_mc_number = forms.CharField(
        max_length=10,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Upper Limit MC Number, e.g: 1600300'})
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
    min_delay = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={'placeholder': 'Minimum Delay'}),
    )
    max_delay = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={'placeholder': 'Maximum Delay'}),
    )
    power_units_comparison = forms.ChoiceField(
        choices=[
            ('lt', 'Less than'),
            ('eq', 'Equal to'),
            ('gt', 'Greater than')
        ],
        required=False,
        label="Power Units (Comparison)"
    )
    power_units_value = forms.IntegerField(
        required=False,
        label="Power Units (Value)",
        widget=forms.NumberInput(attrs={'placeholder': 'Enter number of power units'})
    )
    drivers_comparison = forms.ChoiceField(
        choices=[
            ('lt', 'Less than'),
            ('eq', 'Equal to'),
            ('gt', 'Greater than')
        ],
        required=False,
        label="Drivers (Comparison)"
    )
    drivers_value = forms.IntegerField(
        required=False,
        label="Drivers (Value)",
        widget=forms.NumberInput(attrs={'placeholder': 'Enter number of drivers'})
    )
    status = forms.ChoiceField(
        choices=[
            ('', '---------'), # Optional empty choice for "any"
            ('ACTIVE', 'Active'),
            ('OUT-OF-SERVICE', 'Out-of-Service'),
        ],
        required=False,
        label="Status"
    )
    carrier_operation = forms.ChoiceField(
        choices=[
            ('', '---------'), # Optional empty choice for "any"
            ('Interstate', 'Interstate'),
            ('Intrastate Hazmat', 'Intrastate Hazmat'),
            ('Intrastate Non-Hazmat', 'Intrastate Non-Hazmat'),
        ],
        required=False,
        label="Carrier Operation"
    )
    cargo_classification_search = forms.CharField(
        max_length=255,
        required=False,
        label="Cargo Classification Search",
        widget=forms.TextInput(attrs={'placeholder': 'e.g., General Freight, Refrigerated Food'})
    )
    cargo_info_search = forms.CharField(
        max_length=255,
        required=False,
        label="Cargo Info Search",
        widget=forms.TextInput(attrs={'placeholder': 'e.g., Straight Trucks, Truck Tractors, Trailers etc.'})
    )
    schedule_launch_datetime = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(),
    )
    skip_mc_numbers = forms.CharField(
        required=False,
        label="Skip These MC Numbers",
        widget=forms.TextInput(attrs={
            'placeholder': 'Type an MC Number and press enter',
            'id': 'skip-mc-numbers-input',
            'class': 'rounded-lg outline-none text-primary bg-primary w-full'
        })
    )
    track_campaign = forms.BooleanField(
        required=False,
        label="Track Email Opens",
        widget=forms.CheckboxInput()
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)  # Get user instance
        super().__init__(*args, **kwargs)

        if self.user and self.user.on_free_trial:
            self.fields['mc_number'].widget.attrs['disabled'] = True

    def clean(self):
        cleaned_data = super().clean()

        file_upload = cleaned_data.get('file_upload')
        lower_limit_mc_number = cleaned_data.get('lower_limit_mc_number')
        upper_limit_mc_number = cleaned_data.get('upper_limit_mc_number')
        mc_number = cleaned_data.get('mc_number')
        targets_count = cleaned_data.get('targets_count')
        min_delay = cleaned_data.get('min_delay')
        max_delay = cleaned_data.get('max_delay')
        schedule_launch_datetime = cleaned_data.get('schedule_launch_datetime')

        if self.user and self.user.on_free_trial:
            if not file_upload:
                self.add_error('file_upload', "Free trial users must upload an Excel file.")
        else:
            if not file_upload and not mc_number and not (lower_limit_mc_number and upper_limit_mc_number):
                raise forms.ValidationError("Either upload an Excel file or provide an MC number.")

        if targets_count is not None and targets_count < 1:
            self.add_error('targets_count', "Targets count cannot be less than 1.")

        if min_delay is None:
            min_delay = 30
            cleaned_data['min_delay'] = min_delay

        if max_delay is None:
            max_delay = 60
            cleaned_data['max_delay'] = max_delay

        # Delay validation
        if min_delay < 0:
            self.add_error('min_delay', "Lower limit delay must be greater than 0.")

        if max_delay < min_delay:
            self.add_error('max_delay', "Upper limit delay must be greater than lower limit.")

        # MC number range validation
        if lower_limit_mc_number is not None and upper_limit_mc_number is not None:
            if lower_limit_mc_number > upper_limit_mc_number:
                self.add_error('upper_limit_mc_number', "Upper limit MC Number must be greater than the lower limit.")

        # Schedule datetime validation
        if schedule_launch_datetime:
            now_utc = timezone.now()
            if timezone.is_naive(schedule_launch_datetime):
                try:
                    user_timezone = pytz.timezone(cleaned_data.get('user_timezone', 'Asia/Karachi'))
                    scheduled_time = timezone.make_aware(schedule_launch_datetime, user_timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    scheduled_time = timezone.make_aware(schedule_launch_datetime, timezone.get_current_timezone())
            else:
                scheduled_time = schedule_launch_datetime

            scheduled_time_utc = scheduled_time.astimezone(dt_timezone.utc)
            if scheduled_time_utc <= now_utc:
                self.add_error('schedule_launch_datetime', "Scheduled time must be in the future.")
            else:
                # Store as Karachi time for scheduling
                cleaned_data['schedule_launch_datetime'] = scheduled_time.astimezone(pytz.timezone('Asia/Karachi'))

        return cleaned_data


class BulkCampaignForm(CampaignForm):
    
    select_all = forms.BooleanField(required=False, label="Select all accounts")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        self.total_leads = kwargs.pop('total_leads', 0)  # Total leads passed from view
        super().__init__(*args, user=self.user, **kwargs)

        # Set all fields as not required for Step 2
        for field_name in ['file_upload', 'mc_number', 'lower_limit_mc_number', 'upper_limit_mc_number', 'targets_count', 'email_subject', 'email_body', 'min_delay', 'max_delay', 'select_all',
                          'power_units_comparison', 'power_units_value', 'drivers_comparison', 'drivers_value',
                          'status', 'carrier_operation', 'hm', 'hhg', 'new_entrant', 'cargo_classification_search', 'cargo_info_search']:
            if field_name in self.fields:
                self.fields[field_name].required = False

    def clean(self):
        cleaned_data = self.cleaned_data  # get initial cleaned_data without triggering parent clean

        # Manual validation for delay field
        min_delay = cleaned_data.get('min_delay')
        max_delay = cleaned_data.get('max_delay')
        schedule_launch_datetime = cleaned_data.get('schedule_launch_datetime')

        # Set defaults if left blank
        if min_delay is None:
            min_delay = 30
            cleaned_data['min_delay'] = min_delay
        if max_delay is None:
            max_delay = 60
            cleaned_data['max_delay'] = max_delay

        # Validation rules
        if min_delay < 0:
            self.add_error('min_delay', "Lower limit delay must be greater than 0.")
        if max_delay < min_delay:
            self.add_error('max_delay', "Upper limit delay must be greater than lower limit.")

        # Schedule datetime validation
        if schedule_launch_datetime:
            now_utc = timezone.now()
            if timezone.is_naive(schedule_launch_datetime):
                try:
                    user_timezone = pytz.timezone(cleaned_data.get('user_timezone', 'Asia/Karachi'))
                    scheduled_time = timezone.make_aware(schedule_launch_datetime, user_timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    scheduled_time = timezone.make_aware(schedule_launch_datetime, timezone.get_current_timezone())
            else:
                scheduled_time = schedule_launch_datetime

            scheduled_time_utc = scheduled_time.astimezone(dt_timezone.utc)
            if scheduled_time_utc <= now_utc:
                self.add_error('schedule_launch_datetime', "Scheduled time must be in the future.")
            else:
                # Store as Karachi time for scheduling
                cleaned_data['schedule_launch_datetime'] = scheduled_time.astimezone(pytz.timezone('Asia/Karachi'))


        # Bypass account allocation check if 'select_all' is true
        is_select_all = self.data.get('select_all') in ['true', 'on', '1']

        # Only validate account allocation if 'submit_allocation' is present and 'select_all' is NOT active
        if 'submit_allocation' in self.data and not is_select_all:
            selected_ids = self.data.getlist('selected_accounts')
            if not selected_ids:
                raise forms.ValidationError("Please select at least one email account.")

            assigned_leads = 0
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
                        assigned_leads += int_val
                    except ValueError:
                        self.add_error(None, f"Invalid number format for account ID {account_id}.")

            # Validate that assigned leads match total leads
            if assigned_leads != self.total_leads:
                self.add_error(None, f"ERROR! You assigned {assigned_leads} leads to email accounts, but {self.total_leads} leads are available from the selected lead source. Please resubmit the leads and make sure the numbers match this time.")

