from django.urls import path, include
from .views import *
from . import api

app_name = 'users'

urlpatterns = [
    path("signup/", signup_view, name="signup"),
    path("profile/edit/", user_profile_view, name="user_profile_edit"),
    path('verify-dns/', verify_tracking_dns, name='verify_tracking_dns'),

    # path("login/", email_login_view, name='login'),
    # path("login/verify/", otp_verification_view, name='otp_verify'),

    path("accounts/password_reset/", CustomPasswordResetView.as_view(), name="password_reset"),
		path("accounts/password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", CustomPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    # path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    path("accounts/", include("django.contrib.auth.urls")),  # Include remaining auth views

    # --- Desktop API Endpoints ---
    path('api/desktop/login/', api.DesktopLoginView.as_view(), name='desktop_login'),
    path('api/desktop/heartbeat/', api.DesktopHeartbeatView.as_view(), name='desktop_heartbeat'),
    path('api/desktop/validate-scrape/', api.ValidateScrapeRequestView.as_view(), name='validate_scrape_request'),
]

