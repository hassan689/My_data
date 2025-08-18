from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .tasks import activate_warmup_campaign
from .models import WarmupTemplateSet
from users.models import EmailAccount

def start_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)
    sender_account.is_warmup_target = True
    sender_account.save(update_fields=['is_warmup_target'])
    
    try:
        template_set = WarmupTemplateSet.objects.get(name='Warmup Campaigns Templates')
    except WarmupTemplateSet.DoesNotExist:
        messages.error(request, "Default warmup template set not found.")
        return redirect('dashboard:index')
    
    # Trigger the Celery task to begin the warmup process
    activate_warmup_campaign.delay(sender_account.id, template_set.id)
    
    messages.success(request, f"Warmup campaign for {sender_account.email_address} has been started.")
    return redirect('dashboard:index')


