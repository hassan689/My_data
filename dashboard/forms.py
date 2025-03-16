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
