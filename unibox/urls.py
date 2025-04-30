from django.urls import path, include
from .views import *

app_name = 'unibox'

urlpatterns = [
  path('', inbox_page, name='index'), # Unibox page
  path("threads/", index, name="inbox_json"),  # JSON endpoint
  path('configure-imap/<int:email_account_id>/', add_imap_settings, name='add_imap_settings'),
]

