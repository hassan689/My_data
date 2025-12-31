from django.db.models import Count, Sum, Q, OuterRef, Subquery, DecimalField, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from drip_campaigns.utilities import normalize_provider
from collections import Counter

from dashboard.models import CampaignRecord
from drip_campaigns.models import DripCampaign, SentDripEmail
from subscriptions.models import Subscription, Revenue, Expense
from users.models import CustomUser, EmailAccount
from warmup.models import WarmupCampaign


def get_global_outreach_stats():
    """
    Aggregates stats from CampaignRecord (Blasts) and DripCampaign/SentDripEmail.
    """
    # ... (Existing Status Counts logic remains the same) ...
    blast_stats = CampaignRecord.objects.aggregate(
        launched=Count('id', filter=Q(status='launched')),
        processing=Count('id', filter=Q(status='processing')),
        pending=Count('id', filter=Q(status='pending')),
        total=Count('id'),
        total_sent_raw=Sum('sent_count') # [NEW] Get total sent regardless of tracking
    )
    
    drip_stats = DripCampaign.objects.aggregate(
        active=Count('id', filter=Q(status='Active')), 
        processing=Count('id', filter=Q(status='Processing')),
        paused=Count('id', filter=Q(status='Paused')),
        completed=Count('id', filter=Q(status='Completed')),
        total=Count('id')
    )
    
    # 1. Global Breakdown
    global_stats = {
        'total_campaigns': (blast_stats['total'] or 0) + (drip_stats['total'] or 0),
        'launched_active': (blast_stats['launched'] or 0) + (drip_stats['active'] or 0) + (drip_stats['completed'] or 0),
        'processing': (blast_stats['processing'] or 0) + (drip_stats['processing'] or 0),
        'scheduled_pending': (blast_stats['pending'] or 0) + (drip_stats['paused'] or 0),
    }

    # 2. Tracking Data
    # Blasts: Tracked Only
    blast_tracking = CampaignRecord.objects.filter(track_campaign=True).aggregate(
        total_opens=Sum('open_rate'),
        total_sent=Sum('sent_count')
    )

    drip_tracking_sent = SentDripEmail.objects.count() # Every sent drip is considered "sent"
    drip_tracking_opens = SentDripEmail.objects.filter(is_opened=True).count()
    
    # [NEW] Total Sent (All)
    # Blast raw sent + Drip sent
    total_raw_sent_blasts = blast_stats['total_sent_raw'] or 0
    total_sent_all = total_raw_sent_blasts + drip_tracking_sent

    # Tracked numbers
    total_tracked_sent = (blast_tracking['total_sent'] or 0) + drip_tracking_sent
    total_global_opens = (blast_tracking['total_opens'] or 0) + drip_tracking_opens
    
    global_open_rate = 0
    if total_tracked_sent > 0:
        global_open_rate = round((total_global_opens / total_tracked_sent) * 100, 2)

    return {
        'breakdown': global_stats,
        'tracking': {
            'total_sent_all': total_sent_all, # [NEW]
            'total_sent_tracked': total_tracked_sent,
            'total_opens': total_global_opens,
            'open_rate_percent': global_open_rate
        }
    }


def get_financial_stats():
    """
    Handles Subscriptions, Revenue, and Retention logic.
    """
    now = timezone.localtime(timezone.now()) 
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 1. Subscriptions via Referrals
    subs_via_referral = Subscription.objects.filter(user__referred_by__isnull=False).count()
    
    top_customers = Subscription.objects.exclude(user__is_superuser=True) \
                                        .exclude(paid_amount__isnull=True) \
                                        .order_by('-renewal_count')[:5]

    sub_types = Subscription.objects.values('type').annotate(count=Count('id'))
    sub_status_counts = Subscription.objects.values('status').annotate(count=Count('id'))

    # --- REVENUE & EXPENSES LOGIC ---

    # Prepare Subquery for Total Expenses per month
    expenses_subquery = Expense.objects.filter(
        rev_month_id=OuterRef('id')
    ).values('rev_month_id').annotate(
        total=Sum('amount')
    ).values('total')

    # Fetch Revenue Objects with Annotation
    # We use Coalesce to turn None into 0.00 for months with no expenses
    revenue_qs = Revenue.objects.annotate(
        total_expenses=Coalesce(
            Subquery(expenses_subquery, output_field=DecimalField()), 
            Value(0, output_field=DecimalField())
        )
    ).order_by('month')

    # 1. Current Month Data
    # We filter the already annotated queryset instead of doing a fresh .get()
    # to ensure we have the expense data available.
    curr_revenue_obj = next((r for r in revenue_qs if r.month == current_month_start.date()), None)

    if curr_revenue_obj:
        current_month_stats = {
            'total_revenue': curr_revenue_obj.total_revenue,
            'net_profit': curr_revenue_obj.net_revenue,
            'affiliate_payouts': curr_revenue_obj.paid_to_affiliates,
            # If you want to show explicit total expenses in text, you can use curr_revenue_obj.total_expenses
        }
    else:
        current_month_stats = None

    # 2. Trend Data (All Time)
    trend_data = {
        'labels': [r.month.strftime('%b %Y') for r in revenue_qs],
        'total_revenue': [float(r.total_revenue) for r in revenue_qs],
        'net_profit': [float(r.net_revenue) for r in revenue_qs],
        'affiliate_paid': [float(r.paid_to_affiliates) for r in revenue_qs],
        'total_expenses': [float(r.total_expenses) for r in revenue_qs], # [NEW]
    }

    return {
        'referral_subs_count': subs_via_referral,
        'sub_types': list(sub_types),
        'sub_status_counts': list(sub_status_counts),
        'current_month': current_month_stats,
        'trend_data': trend_data,
        'top_customers': top_customers
    }


def get_user_growth_stats():
    """
    Handles User Signups trends, Referrals, and Normalized Email Infrastructure usage.
    """
    # 1. Total Users (Global)
    total_users = CustomUser.objects.count()
    total_subscribers = Subscription.objects.count()

    # 2. Signups Trend (Group by Month)
    signup_trends = CustomUser.objects.annotate(month=TruncMonth('date_joined')) \
                                      .values('month') \
                                      .annotate(count=Count('id')) \
                                      .order_by('month')
    
    trend_labels = [item['month'].strftime('%b %Y') for item in signup_trends]
    trend_values = [item['count'] for item in signup_trends]

    # 3. Referral Stats (All Time)
    referral_signups = CustomUser.objects.filter(referred_by__isnull=False).count()
    organic_signups = total_users - referral_signups

    # 4. Email Infrastructure Stats (Normalized)
    # Fetch raw providers
    raw_providers = EmailAccount.objects.values_list('email_provider', flat=True)
    
    # Python-side normalization
    provider_counts = Counter()
    for raw_p in raw_providers:
        normalized_name = normalize_provider(raw_p)
        provider_counts[normalized_name] += 1
    
    # Convert Counter to list of dicts for the template
    # Sort by count desc
    sorted_providers = sorted(provider_counts.items(), key=lambda x: x[1], reverse=True)
    provider_stats_ready = [{'provider': k, 'count': v} for k, v in sorted_providers]

    # 5. Infrastructure Counts
    total_accounts = EmailAccount.objects.count()
    warmup_targets = EmailAccount.objects.filter(is_warmup_target=True).count()
    blacklisted = EmailAccount.objects.filter(black_list=True).count()

    return {
        'total_users': total_users,
        'total_subscribers': total_subscribers,
        'signup_trend': {'labels': trend_labels, 'data': trend_values},
        'referral_stats': {'organic': organic_signups, 'referred': referral_signups},
        'provider_stats': provider_stats_ready,
        'infra_stats': {
            'total_accounts': total_accounts,
            'warmup_targets': warmup_targets,
            'blacklisted': blacklisted
        }
    }


