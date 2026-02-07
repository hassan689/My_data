from django.urls import path
from . import views
from users.views import account_groups, delete_group

app_name = 'dashboard'

urlpatterns = [
    path('', views.index, name="index"),
		path('coming-soon/', views.coming_soon, name='coming_soon'),
		path('daily-sheets/', views.daily_sheets_view, name='daily_sheets'),
    path('fmcsa-scraper-download/', views.scraper_donwload, name='scraper_download'),
    path('emails-list-verification/', views.verification_dashboard, name="verification_dashboard"),
    path('emails-list-verification/batch-status-api/', views.batch_status_api, name='batch_status_api'),

		path('campaign/<int:email_account_id>/', views.campaign, name='campaign'),
    path('bulk-campaign/', views.bulk_campaign_step1, name='bulk_campaign'),
    path('bulk-campaign/<str:campaign_key>/', views.bulk_campaign_step2, name='bulk_campaign_step2'),
    path('delete-campaign-records/<int:cmpn_id>/', views.delete_campaign, name='delete_campaign'),

    path('emergency-stop/<int:email_account_id>/', views.emergency_stop, name='emergency_stop'),
    path('resume-stopped/<int:email_account_id>/', views.resume_stopped, name='resume_stopped'),
    path('stop-all-campaigns/', views.stop_all_campaigns, name='stop_all_campaigns'),
    path('campaign-statuses/', views.campaign_statuses, name='campaign_statuses'),

    path('track/<uuid:unique_identifier>/', views.track_open, name='track_open'),
    path('campaign-open-records/export-opens/', views.export_email_opens, name='export_email_opens'),
    path('campaign-open-records/', views.campaign_records, name='campaign_records'),
    
    path('email-account-groups/', account_groups, name="account_groups"),
    path('email-account-groups/delete/<int:group_id>/', delete_group, name="delete_group"),

		path('add-email-account/', views.add_email_account, name='email_account'),
		path('email-account/update/<int:id>/', views.email_account_update, name='email_account_update'),
		path('email-account/delete/<int:id>/', views.email_account_delete, name='email_account_delete'),
    path('email-account/verify-account-dns/<int:account_id>/', views.verify_account_dns, name='verify_account_dns'),

    path('oauth/start/<int:email_account_id>/', views.oauth_start, name='oauth_start'),
    path('oauth/callback/', views.oauth_callback, name='oauth_callback'),
]


