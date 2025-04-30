from django.urls import path, include
from .views import *

app_name = 'unibox'

urlpatterns = [
  path('', index, name='index'),
  path('configure-imap/<int:email_account_id>/', add_imap_settings, name='add_imap_settings'),
]

