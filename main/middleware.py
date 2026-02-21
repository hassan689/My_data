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
    def __init__(self, get_response):
        self.get_response = get_response
        self.system_domains = getattr(settings, 'SYSTEM_DOMAINS', settings.ALLOWED_HOSTS)

    def __call__(self, request):
        host = request.get_host().split(':')[0].lower()

        # 1. System Domain check
        if host in self.system_domains:
            return self.get_response(request)

        cache_key = f"tracking_domain:{host}"
        cache_status = cache.get(cache_key)

        # 2. Fast Exit/Entry from Cache
        if cache_status == 'INVALID':
            return HttpResponseNotFound("Not Found")
        
        if cache_status == 'VALID':
            return self.get_response(request)

        # 3. Check Local DispatchSkool DB
        user_valid = CustomUser.objects.filter(
            tracking_custom_domain=host, 
            tracking_domain_verified=True
        ).exists()

        account_valid = EmailAccount.objects.filter(
            tracking_custom_domain=host,
            tracking_domain_verified=True
        ).exists()

        if user_valid or account_valid:
            cache.set(cache_key, 'VALID', timeout=7200)
            return self.get_response(request)

        # 4. CROSS-APP DELEGATION: Ask ColdSkool if this is their domain
        # Only do this for tracking paths to prevent overhead on other requests
        if request.path.startswith('/track/') or request.path.startswith('/dashboard/track/'):
            try:
                # We use the internal 'check-domain' logic to see if ColdSkool claims it
                resp = requests.get(
                    "https://coldskool.com/check-domain/",
                    params={"domain": host},
                    timeout=2
                )
                
                if resp.status_code == 200:
                    # Validated by ColdSkool! Cache it locally and proxy the request
                    cache.set(cache_key, 'VALID', timeout=7200)
                    return self.proxy_to_coldskool(request, host)
            except requests.RequestException:
                pass

        # 5. Final Fallback: Block and Cache
        cache.set(cache_key, 'INVALID', timeout=3600)
        print(f"Blocked and cached invalid domain: {host}")
        return HttpResponseNotFound("Not Found")

    def proxy_to_coldskool(self, request, host):
        """
        Forwards the request to ColdSkool.
        """
        # Ensure we target the ColdSkool app
        target_url = f"https://coldskool.com{request.get_full_path()}"
        
        try:
            # Forward the original headers so ColdSkool sees the 'track.primeductservices.com' host
            headers = {k: v for k, v in request.headers.items()}
            # Remove Host header so requests uses the target_url domain for routing
            headers.pop('Host', None) 
            
            # Use stream=True for efficiency with the pixel GIF
            response = requests.get(target_url, headers=headers, timeout=5, stream=True)
            
            return HttpResponse(
                response.content, 
                status=response.status_code, 
                content_type=response.headers.get('Content-Type')
            )
        except Exception as e:
            print(f"Proxy Error: {e}")
            return HttpResponseNotFound("Tracking Node Unreachable")
