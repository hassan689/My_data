from django.urls import path
from . import views

app_name = 'warmup'

urlpatterns = [
    path('start-warmup/<int:email_account_id>/', views.start_warmup_view, name="start_warmup_view"),
    path('stop-warmup/<int:email_account_id>/', views.stop_warmup_view, name="stop_warmup_view"),
]

