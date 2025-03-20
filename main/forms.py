from django import forms

class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Your Name'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': 'Your Email'})
    )
    message = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'placeholder': 'Your Message', 'rows': 3})
    )

class PaymentVerificationForm(forms.Form):
    full_name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={'placeholder': 'Your full name'}))
    email = forms.EmailField(widget=forms.EmailInput(attrs={'placeholder': 'your@email.com'}))
    payment_amount = forms.DecimalField(max_digits=10, decimal_places=2, widget=forms.NumberInput(attrs={'placeholder': 'Amount paid'}))
    payment_reference = forms.CharField(max_length=255, widget=forms.TextInput(attrs={'placeholder': 'Transaction ID or reference'}))
    file_upload = forms.FileField(required=True)

