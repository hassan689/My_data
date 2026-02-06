from django import forms
import re
from thefuzz import process, fuzz
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser, Affiliate, AccountGroup, EmailAccount
from django.forms import ValidationError, modelformset_factory

class CustomUserSignupForm(UserCreationForm):
    phone_number = forms.CharField()
    company_name = forms.CharField()
    website_link = forms.URLField(required=False)
    mc_number = forms.CharField(required=False)
    referral_code = forms.CharField(
        max_length=50,
        required=False,
        label="Referral Code"
    )

    class Meta:
        model = CustomUser
        fields = ["username", "email", "first_name", "last_name", "phone_number", "company_name", "website_link", "mc_number", "referral_code", "password1", "password2"]

    def _normalize_code(self, code):
        """
        Helper to strip spaces, special chars and lowercase the string.
        e.g., "Code-123!" -> "code123"
        """
        if not code:
            return ""
        # Remove everything that is NOT alphanumeric (a-z, 0-9)
        return re.sub(r'[^a-zA-Z0-9]', '', code).lower()

    def clean_referral_code(self):
        raw_code = self.cleaned_data.get('referral_code')
        self._referred_by_affiliate = None

        if raw_code:
            # 1. Normalize the user's input
            user_input_clean = self._normalize_code(raw_code)

            # 2. Fetch all affiliates (Efficient enough for <100 records)
            affiliates = Affiliate.objects.all()
            
            # 3. Create a map of { normalized_code: affiliate_obj }
            #    This lets us match against the clean string but retrieve the object
            code_map = {self._normalize_code(a.referral_code): a for a in affiliates}
            
            # 4. Find the best match
            #    process.extractOne returns a tuple: (best_match_string, score)
            best_match = process.extractOne(user_input_clean, code_map.keys(), scorer=fuzz.ratio)

            if best_match:
                match_string, score = best_match
                
                # 5. Threshold Check (80 allows for minor typos/variations)
                if score >= 80:
                    self._referred_by_affiliate = code_map[match_string]
                    # Optional: return the "corrected" code to the form so it saves cleanly
                    return self._referred_by_affiliate.referral_code
                
            # If we fall through here, the code exists but didn't match closely enough,
            # or it was total gibberish. Raise error.
            raise forms.ValidationError("This referral code does not match any affiliate. Please confirm the code and try again.")
            
        return raw_code

    def save(self, commit=True):
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



class UserProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = [
            'first_name', 
            'last_name', 
            'email', 
            'phone_number', 
            'company_name', 
            'tracking_custom_domain'
        ]
        widgets = {
            'tracking_custom_domain': forms.TextInput(attrs={
                'placeholder': 'track.yourcompany.com',
            }),
        }

    def clean_tracking_custom_domain(self):
        domain = self.cleaned_data.get('tracking_custom_domain')
        if domain:
            # Normalize: lower case, remove spaces
            domain = domain.lower().strip()
            # Remove protocol if user pasted it
            domain = domain.replace("https://", "").replace("http://", "")
            # Remove trailing slashes
            domain = domain.rstrip('/')

            # 2. Regex Validation
            # This handles multiple subdomains (e.g., sub.track.site.com)
            domain_regex = r'^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$'

            if not re.match(domain_regex, domain):
                raise ValidationError("Invalid domain format. Please enter a valid domain like 'track.yourcompany.com'.")
                
            return domain


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

