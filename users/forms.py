from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser, Affiliate, AccountGroup, EmailAccount
from django.forms import modelformset_factory

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


class AccountGroupForm(forms.ModelForm):
    
    def __init__(self, *args, **kwargs):
        """
        Expects a 'user' kwarg to be passed from the view
        (e.g., form = AccountGroupForm(request.POST, user=request.user))
        """
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    class Meta:
        model = AccountGroup
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={
                'placeholder': 'e.g., "High-Volume Senders"',
            })
        }

    def clean_name(self):
        """
        Ensure the group name is unique for *this* user.
        """
        name = self.cleaned_data.get('name')
        if not self.user:
            raise forms.ValidationError("User not provided.")

        query = AccountGroup.objects.filter(user=self.user, name__iexact=name)
        
        # Exclude the current instance if we are editing
        if self.instance and self.instance.id:
            query = query.exclude(id=self.instance.id)

        if query.exists():
            raise forms.ValidationError(
                "You already have a group with this name. Please choose a different one."
            )
            
        return name

    def save(self, commit=True):
        """
        Override save to automatically assign the user.
        """
        instance = super().save(commit=False)
        instance.user = self.user

        if commit:
            instance.save()
            
        return instance


class EmailAccountGroupAssignmentForm(forms.ModelForm):
    """
    A form for a single EmailAccount to update its 'group'.
    """
    class Meta:
        model = EmailAccount
        fields = ['account_group']
        widgets = {
            'account_group': forms.RadioSelect(attrs={
                'class': 'hidden-radio peer' 
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # When the form is part of a formset, the user is on the instance
        # (self.instance is one EmailAccount)
        user = self.instance.user
        
        self.fields['account_group'].queryset = AccountGroup.objects.filter(user=user)
        # Remove the blank "----" option
        self.fields['account_group'].empty_label = None

# Create the FormSet to edit ALL accounts at once
EmailAccountAssignmentFormSet = modelformset_factory(
    EmailAccount,
    form=EmailAccountGroupAssignmentForm,
    extra=0  # Don't show any new, empty forms
)



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

