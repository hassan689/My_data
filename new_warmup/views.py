from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.utils import timezone
from users.models import EmailAccount
from .models import WarmupProfile, DailyStat
from .tasks import send_single_warmup_email
import random


def start_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)

    if sender_account.black_list:
        messages.warning(request, f"The email account {sender_account.email_address} has been removed from the warmup pool because of low health. You can still launch launch your campaigns.")
        return redirect('dashboard:index')

    try:
        # 1. Get or Create the Profile
        profile, created = WarmupProfile.objects.get_or_create(
            email_account=sender_account,
            defaults={
                'status': 'Warming', 
                'warmup_enabled': True
            }
        )

        # 2. Update status if it already existed but was paused
        if not created:
            profile.status = 'Warming'
            profile.warmup_enabled = True
            profile.save(update_fields=['status', 'warmup_enabled'])

        # 3. Kickstart the engine immediately.
        # If we don't do this, it will wait until the 9 AM Orchestrator runs.
        today = timezone.now().date()
        stat, _ = DailyStat.objects.get_or_create(profile=profile, date=today)
        
        # Calculate today's volume (current_daily +/- 20% variance)
        base_volume = min(profile.current_daily, profile.daily_limit)
        variance = int(base_volume * 0.2)
        today_target = base_volume + random.randint(-variance, variance)

        if stat.sent < today_target:
            send_single_warmup_email.delay(profile.id, today_target)

        messages.success(request, f"Warmup started for {sender_account.email_address}.")
        
    except Exception as e:
        print("Error starting warmup:", str(e))
        messages.error(request, "Failed to start warmup.")

    return redirect('dashboard:index')


def stop_warmup_view(request, email_account_id):
    sender_account = get_object_or_404(EmailAccount, id=email_account_id)

    try:
        # 1. Flip the switch on the profile
        profile = sender_account.warmup_profile
        profile.status = 'Paused'
        profile.warmup_enabled = False
        profile.save(update_fields=['status', 'warmup_enabled'])
        
    except WarmupProfile.DoesNotExist:
        pass 

    messages.success(request, f"Warmup stopped for {sender_account.email_address}.")
    return redirect('dashboard:index')
