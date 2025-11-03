from datetime import timedelta
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404

from dashboard.utilities import process_excel_file, get_leads_from_db, save_temp_file, distribute_leads_among_accounts
from users.models import EmailAccount
from .models import DripCampaign, EmailAccountAndLeads, DripTemplate

from django.forms import modelformset_factory
from dashboard.forms import BulkCampaignForm
from .forms import DripTemplateModelForm

from django.core.cache import cache
from django.contrib import messages
from django.db import transaction

import uuid
import os


def index(request):
    return render(request, 'drip_campaigns/index.html')


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
                leads = process_excel_file(file_upload)
            elif (mc_number and not request.user.on_free_trial) or ((lower_limit_mc_number and upper_limit_mc_number) and not request.user.on_free_trial):
                lead_source = 'DB'
                leads = get_leads_from_db(
                    mc_number, targets_count, lower_limit_mc_number, upper_limit_mc_number,
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
              refetched_leads = process_excel_file(f)

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
            print("🎉 Redirecting to dashboard:index")
            return redirect('dashboard:index') # temp till we have a campaigns list view

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

