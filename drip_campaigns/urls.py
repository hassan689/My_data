from django.urls import path
from . import views

app_name = 'drip_campaigns'

urlpatterns = [
    path('', views.index, name="index"),

    path('campaign-creator-1/', views.drip_campaign_step1, name='campaign_creator_step1'),
    path('campaign-creator-2/<str:campaign_key>/', views.drip_campaign_step2, name='campaign_creator_step2'),
    path('campaign-creator-3/<int:campaign_id>/', views.drip_campaign_step3, name='campaign_creator_step3'),
]

