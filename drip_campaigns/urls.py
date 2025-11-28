from django.urls import path
from . import views

app_name = 'drip_campaigns'

urlpatterns = [
    path('', views.index, name="index"),

    path('campaign-creator-1/', views.drip_campaign_step1, name='campaign_creator_step1'),
    path('campaign-creator-2/<str:campaign_key>/', views.drip_campaign_step2, name='campaign_creator_step2'),
    path('campaign-creator-3/<int:campaign_id>/', views.drip_campaign_step3, name='campaign_creator_step3'),

    path('update-campaign/<int:campaign_id>/', views.update_drip, name='update_drip'),
    path('view-campaign/<int:campaign_id>/', views.view_drip, name='view_drip'),
    path('campaign-progress/<int:campaign_id>/', views.get_drip_progress_json, name='get_drip_progress_json'),
    
    path('delete-campaign/<int:campaign_id>/', views.delete_drip, name='delete_drip'),
    path('track-campaign/<uuid:unique_identifier>/', views.track_drip, name='track_drip'),

    # Campaign-level controls
    path('<int:campaign_id>/pause/', views.pause_campaign, name='pause_campaign'),
    path('<int:campaign_id>/resume/', views.resume_campaign, name='resume_campaign'),
    path('<int:campaign_id>/cancel/', views.cancel_campaign, name='cancel_campaign'),
    
    # Template-level control
    path('<int:campaign_id>/skip_step/', views.skip_template_step, name='skip_template_step'),
    
    # Account-level control
    path('<int:campaign_id>/stop_account/<int:account_info_id>/', views.stop_account_chain, name='stop_account_chain'),
]

