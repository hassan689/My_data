from django import forms
from .models import DripTemplate
from django_ckeditor_5.widgets import CKEditor5Widget # Import the widget

class DripTemplateModelForm(forms.ModelForm):
    
    subject = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Hello [Legal Name] - [MC Number] - Some Big Offer'})
    )
    body = forms.CharField(
        widget=CKEditor5Widget(config_name='default'),
        required=True
    )
    track_template = forms.BooleanField(
        required=False,
        label="Track Email Opens",
        widget=forms.CheckboxInput()
    )
    class Meta:
        model = DripTemplate
        fields = ['subject', 'body', 'track_template']



