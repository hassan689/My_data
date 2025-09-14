from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser, Affiliate

class CustomUserSignupForm(UserCreationForm):
    phone_number = forms.CharField()
    company_name = forms.CharField()
    website_link = forms.URLField(required=False)
    mc_number = forms.CharField(required=False)
    referral_code = forms.CharField(
        max_length=50,
        required=False,
        label="Referral Code" # Friendly label for the template
    )

    class Meta:
        model = CustomUser
        fields = ["username", "email", "first_name", "last_name", "phone_number", "company_name", "website_link", "mc_number", "referral_code", "password1", "password2"]

    def clean_referral_code(self):
        code = self.cleaned_data.get('referral_code')
        
        if code:
            try:
                affiliate = Affiliate.objects.get(referral_code__iexact=code)
                self._referred_by_affiliate = affiliate 
            except Affiliate.DoesNotExist:
                raise forms.ValidationError("This referral code does not match any affiliate. Please confirm the code and try again.")
        else:
            self._referred_by_affiliate = None
            
        return code

    def save(self, commit=True):
        
        # This creates the CustomUser instance but doesn't save it to the DB yet if commit=False
        user = super().save(commit=False)

        if hasattr(self, '_referred_by_affiliate') and self._referred_by_affiliate:
            user.referred_by = self._referred_by_affiliate
        
        if commit:
            user.save()
        return user


# Uncomment the following code to enable email + OTP login functionality for future use. Not required now as. Business decision.

# class EmailLoginForm(forms.Form):
#     email = forms.EmailField(
#         label="Email Address",
#         max_length=254,
#         required=True,
#     )
#     password = forms.CharField(
#         label="Password",
#         widget=forms.PasswordInput,
#         required=True,
#     )

# class OTPForm(forms.Form):
#     otp_code = forms.CharField(
#         label="OTP",
#         max_length=6,
#         min_length=6,
#         required=True,
#     )

