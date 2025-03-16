from django.urls import path, include
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.index, name="index"),
		path('campaign/', views.campaign, name='campaign'),
		path('email_account/', views.email_account, name='email_account'),
]


