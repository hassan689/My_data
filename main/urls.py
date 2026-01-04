from django.urls import path
from . import views

app_name = 'main'

urlpatterns = [
    path('', views.index, name="index"),
		path('pricing', views.price_page, name='price_page'),
    path('privacy/', views.privacy_policy, name='privacy_policy'),
    path('terms/', views.terms_of_service, name='terms_of_service'),
    path('ceo-dashboard/', views.bi_dashboard_view, name='cofounder_dashboard'),
]
