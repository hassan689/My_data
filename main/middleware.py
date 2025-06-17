from django.shortcuts import render
from django.conf import settings
from django.urls import reverse, NoReverseMatch # Import NoReverseMatch for robustness


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        MAINTENANCE_MODE = getattr(settings, 'MAINTENANCE_MODE', False)
        # Get LOGIN_URL from settings, with a default fallback
        LOGIN_URL = getattr(settings, 'LOGIN_URL', '/accounts/login/')

        # Define paths that are always allowed during maintenance
        allowed_paths = [
            '/admin/', # Django Admin site (always allowed)
        ]

        # Dynamically add the login URL path to allowed_paths
        try:
            # Attempt to reverse the LOGIN_URL name to get its actual path
            login_path = reverse(LOGIN_URL)
            allowed_paths.append(login_path)
        except NoReverseMatch:
            # If LOGIN_URL is already a path (e.g., '/accounts/login/')
            # or cannot be reversed (e.g., invalid name), add it as is.
            allowed_paths.append(LOGIN_URL)

        # Check if the requested path starts with any of the allowed paths
        for path in allowed_paths:
            # Use request.path_info for a clean path without query strings
            if request.path_info.startswith(path):
                return self.get_response(request)

        # If maintenance mode is active AND the user is NOT a superuser,
        # render the maintenance page.
        # This condition checks for anonymous users OR authenticated non-superusers.
        if MAINTENANCE_MODE and (not request.user.is_authenticated or not request.user.is_superuser):
            return render(request, 'maintenance_page.html', status=503) # 503 Service Unavailable

        # Otherwise, proceed with the normal request
        response = self.get_response(request)
        return response
    
