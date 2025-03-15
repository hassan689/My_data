from django.urls import path, include
from .views import *

app_name = 'users'

urlpatterns = [
    path("signup/", signup_view, name="signup"),
    path("accounts/password_reset/", CustomPasswordResetView.as_view(), name="password_reset"),
    path("accounts/", include("django.contrib.auth.urls")),  # Include remaining auth views
]

