from django.urls import path, include
from .views import *

app_name = 'unibox'

urlpatterns = [
  path('', inbox_page, name='index'), # Unibox page
  path("threads/", index, name="inbox_json"),  # JSON endpoint for all threads
  path("threads/<int:thread_id>/", get_thread_messages, name="get_thread_messages"), # Endpoint for fetching a specific thread
  path('toggle_read/<int:thread_id>/', toggle_read_status, name='toggle_read_status'), # Toggle the read/unread status of a thread
  path('unibox/reply/', reply, name='reply'),
  path('delete_thread/<int:thread_id>/', delete_thread, name='delete_thread'),

  path('configure-imap/<int:email_account_id>/', add_imap_settings, name='add_imap_settings'),
]

