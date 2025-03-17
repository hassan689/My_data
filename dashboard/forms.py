from django import forms
from users.models import EmailAccount

class EmailAccountForm(forms.ModelForm):
    class Meta:
        model = EmailAccount
        fields = ["email_address", "encrypted_password", "email_provider", "port_number", "server_type", "host"]

    encrypted_password = forms.CharField(
        widget=forms.PasswordInput(),
        label="Email Password"
    )
    

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

