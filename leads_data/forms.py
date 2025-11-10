import json
from django import forms
from .models import SkipList

class SkipListForm(forms.ModelForm):
    """
    A form for the SkipList model, adapted to use Tagify.
    
    These inputs are for ADDING new entries, which will be
    appended to the existing list on save. The inputs
    will always load empty.
    """

    mc_numbers = forms.CharField(
        required=False,
        widget=forms.TextInput()
    )
    
    emails = forms.CharField(
        required=False,
        widget=forms.TextInput()
    )

    class Meta:
        model = SkipList
        fields = ['mc_numbers', 'emails']

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)
        
        self.initial['mc_numbers'] = ''
        self.initial['emails'] = ''

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
        
        # Case 2: Already a list (e.g., from form resubmission)
        elif isinstance(data, list):
            if data and isinstance(data[0], dict):
                final_list = [
                    item['value'] for item in data 
                    if isinstance(item, dict) and 'value' in item
                ]
            else:
                final_list = [str(item) for item in data] # Assume list of strings
        
        # Case 3: A single string value (e.g., 'MC 123')
        elif isinstance(data, str):
            final_list = [data]
            
        # Final cleanup: strip whitespace and remove any empty strings
        return [item.strip() for item in final_list if item and item.strip()]

    def clean_mc_numbers(self):
        """
        Clean the new Tagify input, apply the user's
        formatting logic, AND append it to the existing list.
        """
        
        def _format_mc(mc_value):
            """
            Applies the user's specific formatting logic from their example:
            [f"MC {mc}" if not str(mc).startswith("MC") else mc for mc in ...]
            """
            mc_str = str(mc_value).strip()
            if not mc_str:
                return None
            
            # Use upper() for a case-insensitive check
            if not mc_str.upper().startswith("MC"):
                return f"MC {mc_str}" # Add "MC " prefix
            else:
                return mc_str # Return as-is (e.g., "MC123" or "MC 123")

        # 1. Get new items from the form
        new_items_raw = self._clean_tagify_input(self.cleaned_data.get('mc_numbers'))
        
        # 2. Get existing items from the database
        existing_items_raw = []
        if self.instance and self.instance.mc_numbers:
            existing_items_raw = self.instance.mc_numbers
            
        # 3. Combine lists (existing first, then new)
        combined_list_raw = existing_items_raw + new_items_raw
        
        # 4. Format and de-duplicate
        formatted_items = {}
        for item in combined_list_raw:
            formatted_item = _format_mc(item)
            if formatted_item:
                formatted_items[formatted_item] = True
        
        # 5. Return the unique, formatted list
        return list(formatted_items.keys())
    
    def clean_emails(self):
        """
        Clean the new Tagify input AND append it to the existing list.
        """
        # 1. Get the new items submitted in the form
        new_items = self._clean_tagify_input(self.cleaned_data.get('emails'))
        
        # 2. Get the items already in the database from the instance
        existing_items = []
        if self.instance and self.instance.emails:
            existing_items = self.instance.emails
            
        # 3. Combine them and remove duplicates (using a set)
        combined_set = set(existing_items)
        combined_set.update(new_items)
        
        return list(combined_set)
    
