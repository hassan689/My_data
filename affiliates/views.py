from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from users.models import Affiliate
from subscriptions.models import Subscription
from django.utils import timezone
from decimal import Decimal


@login_required
def affiliate_dshbrd(request, aff_name, aff_id):
    
    affiliate = get_object_or_404(Affiliate, id=aff_id, name=aff_name)

    if not (request.user == affiliate.user or request.user.is_superuser):
        return HttpResponse("You are not authorized to view this dashboard.")
    
    referred_users_raw = affiliate.referred_users.all().order_by("-date_joined")
    referred_users_data = []

    # --- Earnings Calculation ---
    monthly_earnings = Decimal('0.00')
    current_month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    eligible_subscriptions = Subscription.objects.filter(
        user__referred_by=affiliate,
        status='active',
        start_date__gte=current_month_start, # Subscription started or renewed this month
        paid_amount__isnull=False
    )

    for sub in eligible_subscriptions:
        if sub.paid_amount and affiliate.commission_percentage:
            commission = sub.paid_amount * (affiliate.commission_percentage / Decimal('100'))
            monthly_earnings += commission

    for user in referred_users_raw:
        # Default values for subscription data if no subscription exists
        sub_start_date = "--/--"
        sub_end_date = "--/--"
        status_display = "--/--"
        renewal_count = "--/--"

        try:
            # Attempt to retrieve the associated Subscription object
            subscription = user.subscription
            sub_start_date = subscription.start_date.strftime("%Y-%m-%d %H:%M") if subscription.start_date else "--/--"
            sub_end_date = subscription.end_date.strftime("%Y-%m-%d %H:%M") if subscription.end_date else "--/--"
            status_display = subscription.status
            renewal_count = subscription.renewal_count
        except Subscription.DoesNotExist:
            pass
        except Exception as e:
            print(f"Error retrieving subscription for user {user.username}: {e}")
            pass
        
        referred_users_data.append({
            'company_name': user.company_name,
            'date_joined': user.date_joined.strftime("%Y-%m-%d %H:%M") if user.date_joined else "--/--",
            'subscription_start_date': sub_start_date,
            'subscription_end_date': sub_end_date,
            'status': status_display,
            'renewal_count': renewal_count
        })

    context = {
        'affiliate': affiliate,
        'referred_users_data': referred_users_data,
        'monthly_earnings': monthly_earnings,
        'lifetime_earnings': affiliate.lifetime_earnings,
    }
    return render(request, 'affiliates/affiliate_dshbrd.html', context)


