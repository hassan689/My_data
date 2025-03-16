from django.urls import path, include
from .views import *

app_name = 'users'

urlpatterns = [
    path("signup/", signup_view, name="signup"),
    path("accounts/password_reset/", CustomPasswordResetView.as_view(), name="password_reset"),
		path("accounts/password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", CustomPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("accounts/", include("django.contrib.auth.urls")),  # Include remaining auth views
]

