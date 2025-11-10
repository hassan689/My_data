from django.urls import path
from . import views

app_name = 'leads_data'

urlpatterns = [
    path('skip-lists/', views.skip_list_page, name="skip_list_page"),
]


