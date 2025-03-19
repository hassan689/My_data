from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser

class CustomUserSignupForm(UserCreationForm):
    phone_number = forms.CharField(required=False)
    company_name = forms.CharField()
    website_link = forms.URLField(required=False)
    mc_number = forms.CharField(required=False)

    class Meta:
        model = CustomUser
        fields = ["username", "email", "phone_number", "company_name", "website_link", "mc_number", "password1", "password2"]
