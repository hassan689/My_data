from django.urls import path, include
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.index, name="index"),
		path('coming-soon/', views.coming_soon, name='coming_soon'),
		path('daily-sheets/', views.daily_sheets_view, name='daily_sheets'),

		path('campaign/<int:email_account_id>/', views.campaign, name='campaign'),
		path('add-email-account/', views.add_email_account, name='email_account'),
		path('email-account/update/<int:id>/', views.email_account_update, name='email_account_update'),
		path('email-account/delete/<int:id>/', views.email_account_delete, name='email_account_delete'),
]


