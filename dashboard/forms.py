from django import forms
from users.models import EmailAccount

class EmailAccountForm(forms.ModelForm):
    decrypted_password = forms.CharField(
        widget=forms.PasswordInput(render_value=True),
        label="Email Password",
        required=False
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
        widget=forms.TextInput(attrs={'placeholder': 'Enter Your Email Subject title'})
    )
    email_body = forms.CharField(
        widget=forms.Textarea(attrs={'placeholder': 'Write your Email Body here...', 'class': 'h-28'})
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

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)  # Get user instance
        super().__init__(*args, **kwargs)

        if self.user and self.user.on_free_trial:
            self.fields['mc_number'].widget.attrs['disabled'] = True  # Restrict free trial users

    def clean(self):
        cleaned_data = super().clean()
        file_upload = cleaned_data.get('file_upload')
        mc_number = cleaned_data.get('mc_number')

        if self.user and self.user.on_free_trial:
            if not file_upload:
                self.add_error('file_upload', "Free trial users must upload an Excel file.")
        else:
            if not file_upload and not mc_number:
                raise forms.ValidationError("Either upload an Excel file or provide an MC number.")

        return cleaned_data

