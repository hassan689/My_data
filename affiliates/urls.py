from django.urls import path
from . import views

app_name = 'affiliates'

urlpatterns = [
    path('<str:aff_name>/<int:aff_id>/', views.affiliate_dshbrd, name='affiliate_dshbrd'),
]


