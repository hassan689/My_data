from django.urls import path, include
from . import views

app_name = 'main'

urlpatterns = [
    path('', views.index, name="index"),
		path('pricing', views.price_page, name='price_page')
]
