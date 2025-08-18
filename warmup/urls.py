from django.urls import path
from . import views

app_name = 'warmup'

urlpatterns = [
    path('<int:email_account_id>/', views.start_warmup_view, name="start_warmup_view"),
]

