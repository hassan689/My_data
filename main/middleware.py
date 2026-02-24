from django.shortcuts import render
from django.core.cache import cache
from django.http import HttpResponseNotFound
from users.models import CustomUser, EmailAccount
from django.conf import settings
import requests
from django.http import HttpResponse
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
    

class CustomDomainTrackingMiddleware:
    """
    Middleware to route tracking requests to the correct app:
    - DispatchSkool domains → handled locally
    - ColdSkool domains → proxied
    """

    TRACKING_PATHS = (
        '/track/',
        '/dashboard/track/',
        '/track-campaign/',
        '/drip_campaigns/track-campaign/'
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self.system_domains = getattr(settings, 'SYSTEM_DOMAINS', settings.ALLOWED_HOSTS)

    def __call__(self, request):
        host = request.get_host().split(':')[0].lower()
        cache_key = f"tracking_domain:{host}"

        # 1. System domain → local
        if host in self.system_domains:
            return self.get_response(request)

        # 2. Fast cache lookup
        cache_status = cache.get(cache_key)
        if cache_status == 'INVALID':
            return HttpResponseNotFound("Not Found")
        if cache_status == 'VALID_LOCAL':
            return self.get_response(request)
        if cache_status == 'VALID_COLD':
            return self.proxy_to_coldskool(request, cache_key)

        # 3. Check local DispatchSkool DB
        user_valid = CustomUser.objects.filter(
            tracking_custom_domain=host,
            tracking_domain_verified=True
        ).exists()
        account_valid = EmailAccount.objects.filter(
            tracking_custom_domain=host,
            tracking_domain_verified=True
        ).exists()

        if user_valid or account_valid:
            cache.set(cache_key, 'VALID_LOCAL', timeout=7200)
            return self.get_response(request)

        # 4. Proxy to ColdSkool for tracking paths only
        if any(request.path.startswith(p) for p in self.TRACKING_PATHS):
            return self.proxy_to_coldskool(request, cache_key)

        # 5. Unknown domain → block
        cache.set(cache_key, 'INVALID', timeout=3600)
        return HttpResponseNotFound("Not Found")

    def proxy_to_coldskool(self, request, cache_key):
        """
        Forward the request to ColdSkool via standard internal routing.
        """
        # 1. Send it directly to ColdSkool's main domain
        target_url = f"https://coldskool.com{request.get_full_path()}"
        
        # 2. Prepare headers (remove original Host so Caddy routes it to ColdSkool)
        headers = dict(request.headers)
        headers.pop('Host', None)
        
        # Optional: Tell ColdSkool the original domain in case you need it for logs
        headers['X-Forwarded-Host'] = request.get_host() 

        try:
            # 3. Use stream=True to efficiently pass the pixel GIF back
            resp = requests.get(target_url, headers=headers, timeout=5, stream=True)
            
            # Only cache as VALID_COLD if ColdSkool actually found the tracking pixel (200 OK)
            if resp.status_code == 200:
                cache.set(cache_key, 'VALID_COLD', timeout=7200)
            else:
                # If ColdSkool returns a 404, don't cache it as a valid ColdSkool domain
                cache.set(cache_key, 'INVALID', timeout=3600)

            return HttpResponse(
                resp.content,
                status=resp.status_code,
                content_type=resp.headers.get('Content-Type', 'image/gif')
            )
        except Exception as e:
            print(f"ColdSkool proxy error for {request.get_host()}: {e}")
            cache.set(cache_key, 'INVALID', timeout=3600)
            return HttpResponseNotFound("Tracking Node Unreachable")

