from datetime import timedelta
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404
from django.forms import modelformset_factory
from django.core.paginator import Paginator
from django.core.cache import cache
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Max
from django.http import JsonResponse

from dashboard.utilities import process_excel_file, get_leads_from_db, save_temp_file, distribute_leads_among_accounts
from users.models import EmailAccount
from .models import DripCampaign, EmailAccountAndLeads, DripTemplate

from dashboard.forms import BulkCampaignForm
from .forms import DripTemplateModelForm, RemovedMCNumbersForm

import uuid
import os
import datetime


@login_required
def index(request):
    
    campaign_list = DripCampaign.objects.filter(launched_by=request.user).order_by('-created_at')

    # Paginate the results, 20 cords per page
    paginator = Paginator(campaign_list, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj
    }

    return render(request, 'drip_campaigns/index.html', context)


######################################### Campaign Creation

@login_required
@require_http_methods(["GET", "POST"])
def drip_campaign_step1(request):
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    form = BulkCampaignForm(user=request.user)

    if request.method == 'POST' and 'submit_leads' in request.POST:
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            
            campaign_key = str(uuid.uuid4())
            cache_key = f"drip_leads_{request.user.id}_{campaign_key}"

            file_upload = form.cleaned_data['file_upload']
            mc_number = form.cleaned_data['mc_number']
            lower_limit_mc_number = form.cleaned_data['lower_limit_mc_number']
            upper_limit_mc_number = form.cleaned_data['upper_limit_mc_number']
            targets_count = form.cleaned_data['targets_count']
            skip_mc_numbers = form.cleaned_data.get("skip_mc_numbers")
            name = form.cleaned_data.get("name")

            # Extra filters from the form
            power_units_comparison = form.cleaned_data.get('power_units_comparison')
            power_units_value = form.cleaned_data.get('power_units_value')
            drivers_comparison = form.cleaned_data.get('drivers_comparison')
            drivers_value = form.cleaned_data.get('drivers_value')
            status = form.cleaned_data.get('status')
            carrier_operation = form.cleaned_data.get('carrier_operation')
            cargo_classification_search = form.cleaned_data.get('cargo_classification_search')
            cargo_info_search = form.cleaned_data.get('cargo_info_search')

            leads = []
            if file_upload:
                lead_source = 'Excel'
                leads = process_excel_file(file_upload, request.user)
            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                lead_source = 'DB'
                leads = get_leads_from_db(
                    request.user, mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
                    power_units_comparison=power_units_comparison, power_units_value=power_units_value, 
                    drivers_comparison=drivers_comparison, drivers_value=drivers_value,
                    status=status, carrier_operation=carrier_operation, skip_mc_numbers=skip_mc_numbers,
                    cargo_classification_search_term=cargo_classification_search, cargo_info_search_term=cargo_info_search
                )

            if not leads:
                messages.error(request, "No valid leads found.")
                return redirect('drip_campaigns:campaign_creator_step1')
            
            # Before setting the cache, delete the old one
            cache.delete(cache_key)

            if lead_source == "Excel":
                # Save file in tmp storage
                tmp_path = save_temp_file(file_upload)
                cache_data = {
                    'name': name,
                    'lead_source': 'Excel',
                    'file_path': tmp_path,
                    'leads_available': len(leads)
                }
            else:
                cache_data = {
                    'lead_source': 'DB',
                    'name': name,
                    'params': {
                        'user': request.user,
                        'starting_mc_number': mc_number,
                        'targets_count': targets_count,
                        'lower_limit_mc_number': lower_limit_mc_number,
                        'upper_limit_mc_number': upper_limit_mc_number,
                        'power_units_comparison': power_units_comparison,
                        'power_units_value': power_units_value,
                        'drivers_comparison': drivers_comparison,
                        'drivers_value': drivers_value,
                        'status': status,
                        'carrier_operation': carrier_operation,
                        'skip_mc_numbers': skip_mc_numbers,
                        'cargo_classification_search_term': cargo_classification_search,
                        'cargo_info_search_term': cargo_info_search
                    },
                    'leads_available': len(leads)
                }

            # not storing all the leads in the cache so it doesnt break
            cache.set(cache_key, cache_data, timeout=3600)
            return redirect('drip_campaigns:campaign_creator_step2', campaign_key=campaign_key)

        else:
            messages.error(request, f"Errors: {form.errors}")
            # return redirect(request.path)
            print(form.errors)
        
        
    return render(request, 'drip_campaigns/drip_campaign_step1.html', {
        'form': form,
        'email_accounts': email_accounts,
        'email_accounts_count': email_accounts_count,
    })



@login_required
@require_http_methods(["GET", "POST"])
def drip_campaign_step2(request, campaign_key):
    
    cache_key = f"drip_leads_{request.user.id}_{campaign_key}"
    cached_data = cache.get(cache_key)
    leads_count = cached_data.get('leads_available', 0) if cached_data else 0
    name_of_campaign = cached_data.get('name', '') if cached_data else ''

    # Check if leads are cached from Step 1
    if not cached_data:
        messages.error(request, "Lead data not found. Please start over.")
        return redirect('drip_campaigns:campaign_creator_step1')
    
    email_accounts = EmailAccount.objects.filter(user=request.user)
    email_accounts_count = email_accounts.count()
    form = BulkCampaignForm(user=request.user, total_leads=leads_count) 

    if request.method == 'POST' and 'submit_allocation' in request.POST:
        
        total_leads = cached_data['leads_available'] if cached_data and 'leads_available' in cached_data else 0
        form = BulkCampaignForm(request.POST, request.FILES, user=request.user, total_leads=total_leads)
        if not cached_data:
            messages.error(request, "Lead data not found. Please start over.")
            return redirect('drip_campaigns:campaign_creator_step1')
        
        lead_source = cached_data['lead_source']
        refetched_leads = [] # cause they were fetched once before in the first step

        if lead_source == "Excel":
          file_path = cached_data['file_path']
          with open(file_path, 'rb') as f:
              refetched_leads = process_excel_file(f, request.user)

        elif lead_source == "DB":
            params = cached_data['params']
            refetched_leads = get_leads_from_db(**params)
        
        if form.is_valid():

            leads = refetched_leads

            select_all = form.cleaned_data.get('select_all')
            min_delay = form.cleaned_data.get('min_delay')
            max_delay = form.cleaned_data.get('max_delay')
            
            # this will go into the start_datetime field of the DripCampaign model to start the 1st step
            scheduled_launch_datetime = form.cleaned_data.get('schedule_launch_datetime')
            hours = form.cleaned_data.get('step_delay_hours') or 0
            minutes = form.cleaned_data.get('step_delay_minutes') or 0

            step_delay = timedelta(hours=hours, minutes=minutes)

            lead_source = cached_data.get('lead_source')

            selected_account_ids = request.POST.getlist('selected_accounts')
            account_lead_map = {}
            total_requested_leads = 0

            if select_all:
                # ✅ Only take the accounts that are CHECKED in the form
                selected_ids = request.POST.getlist('selected_accounts')
                accounts = EmailAccount.objects.filter(user=request.user, id__in=selected_ids)

                if not accounts.exists():
                    form.add_error(None, "No email accounts found for your user.")
                    return render(request, 'drip_campaigns/drip_campaign_step2.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })

                # ✅ Auto-distribute leads among the checked accounts only
                account_lead_map = distribute_leads_among_accounts(leads, list(accounts))

            else:
                for account_id in selected_account_ids:
                    try:
                        num_leads = int(request.POST.get(f'emails_for_account_{account_id}', '0'))
                        if num_leads < 1:
                            continue

                        account = EmailAccount.objects.get(id=account_id, user=request.user)
                        account_lead_map[account] = num_leads
                        total_requested_leads += num_leads
                    except (ValueError, EmailAccount.DoesNotExist):
                        continue

                if total_requested_leads != len(leads):
                    form.add_error(None, f"Total assigned leads ({total_requested_leads}) must match total available ({len(leads)}).")
                    return render(request, 'drip_campaigns/drip_campaign_step2.html', {
                        'form': form,
                        'email_accounts': email_accounts,
                        'email_accounts_count': email_accounts_count,
                        'leads_ready': bool(cached_data),
                        'total_leads': len(leads),
                    })

            if not select_all:
                lead_index = 0
                updated_map = {}

                for account, count in account_lead_map.items():
                    if not isinstance(count, int):
                        try:
                            count = int(count[0]) if isinstance(count, list) else int(count)
                        except (ValueError, TypeError):
                            form.add_error(None, f"Invalid lead count for account {account}")
                            return render(request, 'drip_campaigns/drip_campaign_step2.html', {
                                'form': form,
                                'email_accounts': email_accounts,
                                'email_accounts_count': email_accounts_count,
                                'leads_ready': bool(cached_data),
                                'total_leads': len(leads),
                            })

                    updated_map[account] = leads[lead_index:lead_index + count]
                    lead_index += count

                account_lead_map = updated_map

            # Save to Database
            try:
                with transaction.atomic():
                    # 1. Create the parent DripCampaign
                    new_campaign = DripCampaign.objects.create(
                        name=name_of_campaign,
                        launched_by=request.user,
                        min_delay=min_delay,
                        max_delay=max_delay,
                        step_delay=step_delay,
                        next_action_at=scheduled_launch_datetime, 
                        status='Active',
                        lead_source=lead_source,
                        total_recipients=len(leads)
                    )

                    # 2. Create the EmailAccountAndLeads objects in bulk
                    accounts_to_create = []
                    for account, account_leads in account_lead_map.items():
                        accounts_to_create.append(
                            EmailAccountAndLeads(
                                campaign=new_campaign,
                                email_account=account,
                                leads_data=account_leads,
                                # Set initial recipient count for the first step
                                recipient_count=len(account_leads), 
                                sent_count=0 
                            )
                        )
                    
                    EmailAccountAndLeads.objects.bulk_create(accounts_to_create)
            
            except Exception as e:
                # If anything fails, roll back the transaction
                messages.error(request, f"Error saving campaign: {e}. Please try again.")
                return render(request, 'drip_campaigns/drip_campaign_step2.html', {
                    'form': form,
                    'email_accounts': email_accounts,
                    'email_accounts_count': email_accounts_count,
                    'leads_ready': True,
                    'leads_count': leads_count,
                    'name_of_campaign': name_of_campaign,
                })
            
            
            cache.delete(cache_key) # Clean up cache

            # ✅ cleanup temp file after processing
            if cached_data.get("lead_source") == "Excel":
                file_path = cached_data.get("file_path")
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)

            messages.success(request, "Campaign and accounts saved. Now, create your templates.")
            # Redirect to step 3, passing the new campaign's ID
            return redirect('drip_campaigns:campaign_creator_step3', campaign_id=new_campaign.id)

        else:
            return render(request, 'drip_campaigns/drip_campaign_step2.html', {
                'form': form,  # bound form with errors
                'email_accounts': email_accounts,
                'email_accounts_count': email_accounts_count,
                'leads_ready': bool(cached_data),
                'leads_count': leads_count,
            })
        

    return render(request, 'drip_campaigns/drip_campaign_step2.html', {
        'form': form,
        'email_accounts': email_accounts,
        'email_accounts_count': email_accounts_count,
        'leads_ready': bool(cached_data),
        'leads_count': leads_count,
    })



@login_required
def drip_campaign_step3(request, campaign_id):
    
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)
    email_accounts_count = EmailAccount.objects.filter(user=request.user).count()

    # Use modelformset_factory, linked to our new ModelForm
    DripTemplateFormSet = modelformset_factory(
        DripTemplate, 
        form=DripTemplateModelForm, 
        extra=1,  # Show 1 blank form
        can_delete=True
    )

    if request.method == 'POST':
        formset = DripTemplateFormSet(request.POST, prefix='templates')

        if formset.is_valid():
            
            templates_to_create = []
            step_counter = 1

            for form in formset:
                # Check if form has data and is not marked for deletion
                if form.has_changed() and not form.cleaned_data.get('DELETE', False):
                    
                    # Get the instance from the form but don't save to DB yet
                    instance = form.save(commit=False) 
                    
                    instance.campaign = campaign       # Set the foreign key
                    instance.step_number = step_counter # Set the step number
                    
                    templates_to_create.append(instance)
                    step_counter += 1

            if not templates_to_create:
                messages.error(request, "You must add at least one template.")
                return redirect(request.path)

            DripTemplate.objects.bulk_create(templates_to_create)

            messages.success(request, f"Drip campaign '{campaign.name}' successfully created!")
            return redirect('drip_campaigns:index') # temp till we have a campaigns list view

    else:
        # GET request: create a new, empty formset
        formset = DripTemplateFormSet(
            queryset=DripTemplate.objects.none(), 
            prefix='templates'
        )

    return render(request, 'drip_campaigns/drip_campaign_step3.html', {
        'campaign': campaign,
        'formset': formset,
        'email_accounts_count': email_accounts_count
    })


######################################### Campaign View and Update

# --- Define the FormSet ---
# We define this outside the view so it's created only once.
# extra=0: Don't show any new, blank forms by default.
# can_delete=True: Allow existing templates to be marked for deletion.
DripTemplateFormSet = modelformset_factory(
    DripTemplate,
    form=DripTemplateModelForm,
    extra=0,
    can_delete=True 
)

@login_required
@require_http_methods(["GET", "POST"])
def update_drip(request, campaign_id):
    
    # 1. Fetch the main campaign object
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)

    # 2. Get data for display (needed for both GET and POST-failure)
    email_accounts_info = campaign.email_accounts_and_leads.all().select_related('email_account')
    
    # 3. Define the queryset for the formset
    template_queryset = campaign.templates.all().order_by('step_number')

    if request.method == 'POST':
        # 4. Process the submitted data
        # We use prefixes to keep the forms separate in the POST data
        campaign_form = BulkCampaignForm(request.POST, prefix='campaign')
        template_formset = DripTemplateFormSet(request.POST, queryset=template_queryset, prefix='templates')

        mc_form = RemovedMCNumbersForm(request.POST, instance=campaign, prefix='mc_numbers')

        if campaign_form.is_valid() and template_formset.is_valid() and mc_form.is_valid():
            # Save CampaignForm data
            cd = campaign_form.cleaned_data
            hours = cd.get('step_delay_hours') or 0
            minutes = cd.get('step_delay_minutes') or 0
            
            campaign.step_delay = datetime.timedelta(hours=hours, minutes=minutes)
            campaign.min_delay = cd.get('min_delay')
            campaign.max_delay = cd.get('max_delay')
            campaign.save()
            mc_form.save()

            # Get all modified and new instances without saving to DB
            templates = template_formset.save(commit=False)

            # Get the current highest step number for this campaign
            max_step = campaign.templates.all().aggregate(Max('step_number'))['step_number__max'] or 0

            step_counter = 1

            for template in templates:
                # Check if it's a new instance (no primary key)
                if not template.id: 
                    template.campaign = campaign
                    template.step_number = max_step + step_counter
                    step_counter += 1

                # Save the instance (whether new or modified)
                template.save()

            # Handle any deletions
            for form in template_formset.deleted_forms:
                if form.instance.id:
                    form.instance.delete()

            messages.success(request, f"Campaign '{campaign.name}' has been updated successfully.")
            return redirect('drip_campaigns:view_drip', campaign.id) # Assumed URL name
        
        else:
            # If forms are invalid, fall through to render the page again
            # with error messages.
            messages.error(request, "Please correct the errors below.")

    else:
        # 5. Handle GET request: Populate forms with existing data
        
        # Deconstruct the timedelta into hours and minutes
        total_seconds = campaign.step_delay.total_seconds()
        initial_hours = int(total_seconds // 3600)
        initial_minutes = int((total_seconds % 3600) // 60)
        
        # Pre-fill the CampaignForm
        campaign_form = BulkCampaignForm(prefix='campaign', initial={
            'step_delay_hours': initial_hours,
            'step_delay_minutes': initial_minutes,
            'min_delay': campaign.min_delay,
            'max_delay': campaign.max_delay
        })
        mc_form = RemovedMCNumbersForm(instance=campaign, prefix='mc_numbers')
        
        # Pre-fill the DripTemplateFormSet
        template_formset = DripTemplateFormSet(queryset=template_queryset, prefix='templates')

    # 6. Render the page
    context = {
        'campaign': campaign,
        'campaign_form': campaign_form,
        'template_formset': template_formset,
        'email_accounts_info': email_accounts_info,
        'mc_form': mc_form
    }
    
    return render(request, 'drip_campaigns/update_drip.html', context)


@login_required
def view_drip(request, campaign_id): # to display general info and show email accounts and their sent/recip counts
    
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)
    templates_qs = campaign.templates.all().order_by('step_number')
    email_accounts_info = campaign.email_accounts_and_leads.all().select_related('email_account')
    
    return render(request, 'drip_campaigns/view_drip.html', {
        'campaign': campaign,
        'email_accounts_info': email_accounts_info,
        'templates_qs': templates_qs
    })


@login_required
@require_http_methods(["GET"])
def get_drip_progress_json(request, campaign_id):
    """
    Returns the latest sent/recipient counts for a campaign as JSON.
    """
    
    # 1. Get the campaign, ensuring it belongs to the logged-in user
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)
    
    # 2. Get all the associated email account data
    email_accounts_info = campaign.email_accounts_and_leads.all()
    
    # 3. Build the data dictionary in the format your JavaScript expects
    data = {}
    for account in email_accounts_info:
        data[account.email_account.id] = {
            'sent_count': account.sent_count,
            'total': account.recipient_count  # Use 'total' to match your JS
        }
        
    # 4. Return the data as a JSON response
    return JsonResponse(data)


@login_required
def delete_drip(request, campaign_id):
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)
    campaign_name = campaign.name
    campaign.delete()

    messages.success(request, f"Campaign '{campaign_name}' and all its related data have been successfully deleted.")
    return redirect('drip_campaigns:index')


@login_required
@require_http_methods(["GET"])
def track_drip(request, campaign_id):
    
    campaign = get_object_or_404(DripCampaign, id=campaign_id, launched_by=request.user)
    templates_qs = campaign.templates.filter(track_template=True).order_by('step_number')

    recipient_data = EmailAccountAndLeads.objects.filter(campaign=campaign).aggregate(
        total_recipients=Sum('recipient_count')
    )
    campaign_total_recipients = recipient_data['total_recipients'] or 0
    
    campaign_total_opens = 0
    for template in templates_qs:
        campaign_total_opens += template.open_rate

    context = {
        'campaign': campaign,
        'templates_qs': templates_qs,
        'campaign_total_opens': campaign_total_opens,
        'campaign_total_recipients': campaign_total_recipients,
    }
    return render(request, 'drip_campaigns/track_drip.html', context)

