from leads_data.models import Lead, SkipList
from django.db.models import Q, F, Value, IntegerField
from django.db.models.functions import Cast, Replace

from django.conf import settings
from django.http import HttpResponse
from django.core.files.storage import default_storage
from django.core.mail import get_connection

import pandas as pd
import re
import json
import os
import smtplib
from bs4 import BeautifulSoup


def _normalize_mc_value(value):
    """
    Normalize an MC number input to the format 'MC #######' where the numeric part
    is padded with leading zeros to 7 digits if it's shorter than 7 digits.

    Rules:
    - Accepts ints or strings.
    - Strips any leading 'MC' (case-insensitive) and non-digit characters.
    - If no digits found, returns the original stripped string.
    - If found digits length < 7 -> pad left with zeros to 7 digits.
    - If found digits length >= 7 -> keep digits as-is.
    - Returns the normalized string with 'MC ' prefix.
    """
    if value is None:
        return None
    s = str(value).strip()
    # Remove leading 'MC' or 'mc' and surrounding whitespace
    s = re.sub(r'(?i)^mc\s*', '', s)
    # Extract digits only
    digits = re.sub(r'\D', '', s)
    if not digits:
        # Nothing to normalize; return the original trimmed value
        return s
    if len(digits) < 7:
        digits = digits.zfill(7)
    return f"MC {digits}"


def process_leads_file(file, user):
    """
    Processes CSV or XLSX, cleans data, and filters against SkipList.
    Returns a list of dicts.
    """
    ext = file.name.split('.')[-1].lower()
    
    try:
        # 1. Unified Reading Logic
        if ext == 'xlsx':
            df = pd.read_excel(file)
        elif ext == 'csv':
            # Use 'latin1' or 'utf-8-sig' to handle common Excel CSV encoding issues
            df = pd.read_csv(file, encoding='utf-8-sig')
        else:
            return []

        # 2. SkipList Setup
        skip_mcs, skip_emails = set(), set()
        if user and user.is_authenticated:
            sl = SkipList.objects.filter(user=user).first()
            if sl:
                skip_mcs = set(sl.mc_numbers or [])
                skip_emails = set(sl.emails or [])

        # 3. Column Identification
        cols_map = {col: col.strip().lower().replace(" ", "").replace("#", "") for col in df.columns}
        email_col = next((c for c, n in cols_map.items() if 'email' in n), None)
        mc_col = next((c for c, n in cols_map.items() if n in ['mcnumber', 'mc']), None)

        if not email_col:
            return []

        # 4. Processing Helpers
        email_regex = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
        
        def clean(val):
            if pd.isna(val): return ''
            if isinstance(val, float) and val.is_integer(): return str(int(val))
            return str(val).strip()

        leads = []
        
        # 5. Iteration (More scalable than iterrows)
        for row in df.to_dict('records'):
            email_val = clean(row.get(email_col))
            
            # Email Validation & Skip Check
            if not email_val or not email_regex.match(email_val) or email_val in skip_emails:
                continue

            # MC Validation & Skip Check
            mc_formatted = None
            if mc_col:
                mc_raw = clean(row.get(mc_col))
                if mc_raw:
                    mc_formatted = _normalize_mc_value(mc_raw)
                    if mc_formatted in skip_mcs:
                        continue

            # 6. Build Row Dictionary
            # Clean every value in the row while keeping original keys
            lead_item = {str(k): clean(v) for k, v in row.items()}
            
            # Normalize key names for your downstream logic
            lead_item['Email'] = email_val
            if mc_col:
                lead_item[mc_col] = mc_formatted
                
            leads.append(lead_item)

        return leads

    except Exception as e:
        return []


def get_leads_from_db(user, starting_mc_number=None, targets_count=None,
                      lower_limit_mc_number=None, upper_limit_mc_number=None,  # <-- Added support for range
                      power_units_comparison=None, power_units_value=None,
                      drivers_comparison=None, drivers_value=None,
                      status=None, carrier_operation=None, skip_mc_numbers=None,
                      cargo_classification_search_term=None, cargo_info_search_term=None):
    try:
        
        # 1. Get the user's skip lists
        skip_mcs = []
        skip_emails = []
        try:
            skip_list = SkipList.objects.get(user=user)
            skip_mcs = skip_list.mc_numbers or []
            skip_emails = skip_list.emails or []
        except SkipList.DoesNotExist:
            pass  # No list found, just use the empty lists

        # 2. Build the exclusion filter
        # We will exclude any lead where EITHER the mc_number OR the email
        # is in the user's skip lists.
        exclusion_filters = Q()
        if skip_mcs:
            exclusion_filters |= Q(mc_number__in=skip_mcs)
        
        if skip_emails:
            exclusion_filters |= (Q(email__in=skip_emails) & ~Q(email=''))

        # 3. Apply the exclusions to the base queryset
        queryset = Lead.objects.all().exclude(exclusion_filters)
        # queryset = Lead.objects.all()

        if skip_mc_numbers:
            # Check if the input is a JSON string and parse it
            if isinstance(skip_mc_numbers, str) and skip_mc_numbers.startswith('['):
                skip_mc_numbers = json.loads(skip_mc_numbers)
            
            # Now, format the list to get just the values
            skip_mc_numbers_formatted = [item['value'] for item in skip_mc_numbers]
            skip_mc_numbers_formatted = [f"MC {mc}" if not str(mc).startswith("MC") else mc for mc in skip_mc_numbers_formatted]
            queryset = queryset.exclude(mc_number__in=skip_mc_numbers_formatted)

        # Apply numerical cleaning
        queryset = queryset.filter(
            power_units__regex=r'^\s*\d+\s*$',
            drivers__regex=r'^\s*\d+\s*$',
        )

        queryset = queryset.annotate(
            power_units_int=Cast(Replace(Replace(F('power_units'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
            drivers_int=Cast(Replace(Replace(F('drivers'), Value(','), Value('')), Value(' '), Value('')), IntegerField()),
        )

        filters = (
            Q(email__isnull=False) &
            ~Q(email='') &
            ~Q(power_units='') &
            ~Q(drivers='')
        )

        if status and not status == '':
            filters &= Q(status=status)
        if carrier_operation and not carrier_operation == '':
            filters &= Q(carrier_operation=carrier_operation)

        # --- START OF NEW TAGIFY LOGIC FOR CARGO CLASSIFICATION ---
        cargo_classification_list = []
        if cargo_classification_search_term:
            # Parse the JSON from Tagify if it's a string
            if isinstance(cargo_classification_search_term, str) and cargo_classification_search_term.startswith('['):
                try:
                    parsed_list = json.loads(cargo_classification_search_term)
                    cargo_classification_list = [item['value'] for item in parsed_list]
                except json.JSONDecodeError:
                    # Fallback to single string if parsing fails
                    cargo_classification_list = [cargo_classification_search_term]
            elif isinstance(cargo_classification_search_term, list):
                # If it's already a list (from a previous form cleaning step)
                cargo_classification_list = cargo_classification_search_term
            else:
                # Treat as a single string
                cargo_classification_list = [cargo_classification_search_term]

        if cargo_classification_list:
            cargo_classification_filters = Q()
            for term in cargo_classification_list:
                term_lower = term.lower()
                cargo_classification_filters |= Q(
                    cargo_classifications__icontains=term_lower
                )
            queryset = queryset.filter(
                Q(cargo_classifications__isnull=False) & ~Q(cargo_classifications='')
            ).filter(cargo_classification_filters)
        # --- END OF NEW TAGIFY LOGIC ---

        # --- START OF NEW TAGIFY LOGIC FOR CARGO INFO ---
        cargo_info_list = []
        if cargo_info_search_term:
            # Parse the JSON from Tagify if it's a string
            if isinstance(cargo_info_search_term, str) and cargo_info_search_term.startswith('['):
                try:
                    parsed_list = json.loads(cargo_info_search_term)
                    cargo_info_list = [item['value'] for item in parsed_list]
                except json.JSONDecodeError:
                    # Fallback to single string if parsing fails
                    cargo_info_list = [cargo_info_search_term]
            elif isinstance(cargo_info_search_term, list):
                # If it's already a list (from a previous form cleaning step)
                cargo_info_list = cargo_info_search_term
            else:
                # Treat as a single string
                cargo_info_list = [cargo_info_search_term]

        if cargo_info_list:
            cargo_info_filters = Q()
            for term in cargo_info_list:
                term_lower = term.lower()
                cargo_info_filters |= Q(
                    cargo_info__icontains=term_lower
                )
            queryset = queryset.filter(
                Q(cargo_info__isnull=False) & ~Q(cargo_info='')
            ).filter(cargo_info_filters)
        # --- END OF NEW TAGIFY LOGIC ---

        if power_units_comparison and power_units_value is not None:
            filters &= Q(power_units_int__isnull=False)
            if power_units_comparison == 'gt':
                filters &= Q(power_units_int__gte=power_units_value)
            elif power_units_comparison == 'lt':
                filters &= Q(power_units_int__lte=power_units_value)
            elif power_units_comparison == 'eq':
                filters &= Q(power_units_int=power_units_value)

        if drivers_comparison and drivers_value is not None:
            filters &= Q(drivers_int__isnull=False)
            if drivers_comparison == 'gt':
                filters &= Q(drivers_int__gte=drivers_value)
            elif drivers_comparison == 'lt':
                filters &= Q(drivers_int__lte=drivers_value)
            elif drivers_comparison == 'eq':
                filters &= Q(drivers_int=drivers_value)

        queryset = queryset.filter(filters)

        leads = []  # <-- Unified output container

        # ---------- START OF NEW RANGE MODE SUPPORT ----------
        if lower_limit_mc_number and upper_limit_mc_number and not starting_mc_number:
            lower_mc = _normalize_mc_value(lower_limit_mc_number)
            upper_mc = _normalize_mc_value(upper_limit_mc_number)

            lower_bound = (
                queryset.filter(mc_number__gte=lower_mc)
                .order_by('mc_number')
                .first()
            )

            upper_bound = (
                queryset.filter(mc_number__lte=upper_mc)
                .order_by('-mc_number')
                .first()
            )

            if not lower_bound or not upper_bound:
                return []

            leads = list(
                queryset.filter(mc_number__gte=lower_bound.mc_number,
                                mc_number__lte=upper_bound.mc_number)
                .order_by('mc_number')
            )
        # ---------- END OF RANGE MODE ----------

        # ---------- DEFAULT STARTING MC + TARGET COUNT MODE ----------
        elif starting_mc_number and targets_count:
            formatted_mc = _normalize_mc_value(starting_mc_number)

            starting_lead = (
                queryset.filter(mc_number__gte=formatted_mc)
                .order_by('mc_number')
                .first()
            )

            if not starting_lead:
                starting_lead = (
                    queryset.filter(mc_number__lte=formatted_mc)
                    .order_by('-mc_number')
                    .first()
                )

            if not starting_lead:
                return []

            starting_mc = starting_lead.mc_number

            leads_after = list(
                queryset.filter(mc_number__gte=starting_mc)
                .order_by('mc_number')[:targets_count]
            )

            remaining = targets_count - len(leads_after)

            if remaining > 0:
                leads_before = list(
                    queryset.filter(mc_number__lt=starting_mc)
                    .order_by('-mc_number')[:remaining]
                )
                leads_after.extend(leads_before)

            leads = leads_after[:targets_count]
        # ---------- END OF DEFAULT MODE ----------

        else:
            return []

        # ---------- UNIFIED FINAL OUTPUT FORMATTING ----------
        formatted = [
            {
                'MC Number': lead.mc_number,
                'Legal Name': lead.legal_name,
                'Email': lead.email,
                'U.S DOT': lead.usdot,
                'VMT Year': lead.vmt_year,
                'Power Units': lead.power_units,
                'DUNS Number': lead.duns_number,
                'Drivers': lead.drivers,
                'Cargo Classifications': lead.cargo_classifications,
                'Cargo Info': lead.cargo_info,
                'Telephone': lead.telephone,
                'Address': lead.address,
            }
            for lead in leads
        ]

        return formatted

    except Exception as e:
        print(f"Exception: {e}")
        return []


def save_temp_file(uploaded_file):
    """Save uploaded file temporarily in MEDIA_ROOT/tmp/ and return the path."""
    temp_dir = os.path.join(settings.MEDIA_ROOT, "tmp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, uploaded_file.name)

    with default_storage.open(temp_path, 'wb+') as dest:
        for chunk in uploaded_file.chunks():
            dest.write(chunk)

    return temp_path


def distribute_leads_among_accounts(leads, accounts):
    total_leads = len(leads)
    total_accounts = len(accounts)
    base_count = total_leads // total_accounts
    remainder = total_leads % total_accounts

    lead_index = 0
    account_lead_map = {}

    for i, account in enumerate(accounts):
        count = base_count + (1 if i < remainder else 0)
        assigned_leads = leads[lead_index:lead_index + count]
        lead_index += count
        account_lead_map[account] = assigned_leads

    return account_lead_map

def distribute_leads_via_groups(all_leads, group_lead_counts_map):
    """
    Orchestrator: Takes the master list of leads and a map of {Group: count}.
    1. Slices the master list for the group.
    2. Distributes that slice to the accounts within that group.
    3. Returns a flattened master map of {Account: leads}.
    """
    final_account_map = {}
    current_lead_index = 0

    for group, count in group_lead_counts_map.items():
        # 1. Slice the leads for this specific group
        end_index = current_lead_index + count
        group_leads = all_leads[current_lead_index : end_index]
        current_lead_index = end_index

        # 2. Fetch accounts in this group
        group_accounts = list(group.email_accounts.all())

        # 3. Distribute this group's leads among its accounts
        sub_map = distribute_leads_among_accounts(group_leads, group_accounts)
        
        # 4. Update the master map
        final_account_map.update(sub_map)

    return final_account_map


def gif_response():
    response = HttpResponse(
        b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00,\x00\x00\x00\x00'
        b'\x01\x00\x01\x00\x00\x02\x02D\x01\x00;',
        content_type='image/gif'
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def personalize_template(template, lead):
    
    # Find all placeholders like [Some Column]
    placeholders = re.findall(r'\[([^\]]+)\]', template)
    
    for ph in placeholders:
        value = str(lead.get(ph, ph))  # Use the column name as fallback if missing
        template = template.replace(f"[{ph}]", value)
    
    return template


def get_email_connection(email_account, decrypted_password):
    """
    Establishes and opens an SMTP connection for sending emails.
    """
    use_tls = email_account.server_type == "STARTTLS" or email_account.server_type == "TLS"
    use_ssl = email_account.server_type == "SSL"

    if use_tls and use_ssl:
        print("Invalid configuration: Cannot enable all TLS, SSL and STARTTLS.")
        return

    try:
        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=email_account.host,
            port=email_account.port_number,
            username=email_account.email_address,
            password=decrypted_password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=30,  # lower global timeout
        )
        connection.open()
        return connection
    except (TimeoutError, OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as e:
        print(f"[SMTP Timeout] Could not connect to {email_account.email_address}: {e}")
        return None


def sanitize_email_html(html_content, base_url, max_email_width=600):
    """
    1. Converts relative image URLs to absolute URLs.
    2. Keeps the exact pixel width/height set by CKEditor on the <img> tag.
    """
    # Assuming BeautifulSoup is imported correctly
    soup = BeautifulSoup(html_content, 'html.parser')

    # 1. Fix Images and enforce original dimensions
    for img in soup.find_all('img'):
        src = img.get('src')
        
        # Preserve original dimensions from the <img> tag
        original_width = img.get('width')
        original_height = img.get('height')

        if src and src.startswith('/media/'):
            # Make URL absolute
            img['src'] = base_url + src

        # --- REVISED LOGIC STARTS HERE ---
        
        # Get the parent <figure> tag (CKEditor puts width:XX% here)
        figure = img.find_parent('figure')
        
        # 1. Check for CKEditor percentage width on the <figure>
        # This part is now primarily for removing the <figure> style, but 
        # it *can* still calculate the pixel width if needed.
        intended_width_px = None
        if figure and 'style' in figure.attrs:
            style = figure['style']
            match = re.search(r'width:(\d+\.?\d*)%', style)
            
            # If a percentage is found, calculate the intended width (optional, 
            # but good for robust handling).
            if match:
                percentage = float(match.group(1))
                intended_width_px = round(percentage / 100 * max_email_width)
            
            # Always remove the unreliable style from the <figure> tag
            figure.attrs.pop('style', None)

        # 2. **Apply the intended/original width.**
        # Prioritize the width directly on the <img> tag (what CKEditor saved)
        # If CKEditor saved a pixel width, use it. If not, use the max width.
        
        if original_width and original_width.isdigit():
            # Use the exact width saved by CKEditor (e.g., 568 or 305)
            img['width'] = original_width
        elif intended_width_px:
            # Fallback to calculated pixel width from a percentage
            img['width'] = str(intended_width_px)
        else:
            # Fallback to full email width
            img['width'] = str(max_email_width)
            
        # Also re-apply the original height if it was present, or use 'auto'
        if original_height and original_height.isdigit():
            img['height'] = original_height
        else:
            img['height'] = "auto"
            
        # Always remove other unreliable styles from the img tag
        if 'style' in img.attrs:
            del img.attrs['style']
        
        # --- REVISED LOGIC ENDS HERE ---

    # 3. Fix other relative hrefs
    for tag in soup.find_all(href=True):
        href = tag.get('href')
        if href and href.startswith('/media/'):
            tag['href'] = base_url + href
            
    cleaned_html = str(soup)
    wrapped_html = f'<div style="margin:0;padding:0;">{cleaned_html}</div>'
    return wrapped_html


EMAIL_TASK_TIME_LIMIT = 600
def should_use_batch_processing(min_delay: int, max_delay: int, batch_size: int = 20) -> bool:
    """
    Decide whether to use batch processing.
    Conditions:
      - variance small
      - average short enough
      - estimated batch time within safe task runtime
    """
    MAX_VARIANCE_SECONDS = 30
    MAX_AVERAGE_SECONDS = 60
    MAX_BATCH_TASK_TIME = int(EMAIL_TASK_TIME_LIMIT * 0.8)  # keep headroom

    variance = max_delay - min_delay
    average_delay = (max_delay + min_delay) / 2.0
    estimated_batch_time = average_delay * batch_size

    # For debugging/visibility you can log estimated_batch_time here
    print(f"Batch decision: variance={variance}s, average={average_delay}s, estimated_batch_time={estimated_batch_time}s")

    if variance <= MAX_VARIANCE_SECONDS and average_delay <= MAX_AVERAGE_SECONDS and estimated_batch_time <= MAX_BATCH_TASK_TIME:
        return True
    return False


# for the email verification processing
def fill_missing_and_return(chunk_rows, processed_map):
    """Ensures every original row has a status before returning to finalizer."""
    final_chunk = []
    for row in chunk_rows:
        email = str(row.get('Email', '')).strip().lower()
        res = processed_map.get(email, {})
        
        row.update({
            'Status': res.get('result', 'timeout' if email else 'INVALID'),
            'Score': res.get('score', 0),
            'Reason': res.get('reason', 'API Timeout' if email else 'Empty email'),
            'Disposable': res.get('is_disposable', False)
        })
        final_chunk.append(row)
    return final_chunk


