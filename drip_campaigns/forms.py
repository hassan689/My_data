from django import forms
import json
from .models import DripTemplate, DripCampaign


class DripTemplateModelForm(forms.ModelForm):
    
    subject = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Hello [Legal Name] - [MC Number] - Some Big Offer'})
    )
    body = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={
            'class': 'w-full h-96 p-5 bg-slate-900 text-blue-400 rounded-xl font-mono text-sm outline-none border-4 border-slate-800',
            'placeholder': 'Paste your professional HTML here...'
        })
    )
    track_template = forms.BooleanField(
        required=False,
        label="Track Open Rate",
        widget=forms.CheckboxInput(attrs={
            'class': 'w-4 h-4 text-green-600 bg-gray-700 border-gray-600 rounded focus:ring-green-500 focus:ring-2'
        })
    )
    class Meta:
        model = DripTemplate
        fields = ['subject', 'body', 'track_template']


class RemovedMCNumbersForm(forms.ModelForm):
    """
    A form to add new MC Numbers to a DripCampaign's 'removed_mc_numbers'
    list. This form appends data, it does not overwrite.
    """

    # 1. Define the form field. It's a CharField, not a JSONField,
    #    to accept the string-based input from Tagify.
    removed_mc_numbers = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={'placeholder': 'Add MC Numbers to remove...'}
        ),
        label="Add MC Numbers to Remove List"
    )

    class Meta:
        model = DripCampaign
        # This links the form field to the model field
        fields = ['removed_mc_numbers']

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)

        # Always start with an empty input box, regardless of instance data
        self.initial['removed_mc_numbers'] = ''

    def _clean_tagify_input(self, data):
        """
        Parses the various formats Tagify might send (JSON string,
        single string, or already a list) into a clean list of strings.
        """
        if not data:
            return []
        final_list = []
        
        # Case 1: JSON string from Tagify (e.g., '[{"value":"..."}, ...]')
        if isinstance(data, str) and data.startswith('['):
            try:
                parsed_list = json.loads(data)
                final_list = [
                    item['value'] for item in parsed_list 
                    if isinstance(item, dict) and 'value' in item
                ]
            except json.JSONDecodeError:
                final_list = [data] # Fallback
        
        # Case 2: Already a list
        elif isinstance(data, list):
            if data and isinstance(data[0], dict):
                final_list = [
                    item['value'] for item in data 
                    if isinstance(item, dict) and 'value' in item
                ]
            else:
                final_list = [str(item) for item in data]
        
        # Case 3: A single string value
        elif isinstance(data, str):
            final_list = [data]
            
        # Final cleanup
        return [item.strip() for item in final_list if item and item.strip()]

    def clean_removed_mc_numbers(self):
        """
        Cleans the new Tagify input, applies formatting, AND
        appends it to the existing list from the instance.
        """
        
        def _format_mc(mc_value):
            """
            Applies your specific formatting logic (adds "MC " prefix
            if not already present).
            """
            mc_str = str(mc_value).strip()
            if not mc_str:
                return None
            
            # Use upper() for a case-insensitive check
            if not mc_str.upper().startswith("MC"):
                return f"MC {mc_str}" # Add "MC " prefix
            else:
                return mc_str # Return as-is

        # 1. Get new items from the form
        new_items_raw = self._clean_tagify_input(
            self.cleaned_data.get('removed_mc_numbers')
        )
        
        # 2. Get existing items from the database
        existing_items_raw = []
        if self.instance and self.instance.removed_mc_numbers:
            existing_items_raw = self.instance.removed_mc_numbers
            
        # 3. Combine lists (existing first, then new)
        combined_list_raw = existing_items_raw + new_items_raw
        
        # 4. Format and de-duplicate
        formatted_items = {} # Use dict as an ordered set
        for item in combined_list_raw:
            formatted_item = _format_mc(item)
            if formatted_item:
                formatted_items[formatted_item] = True
        
        # 5. Return the unique, formatted list
        return list(formatted_items.keys())



