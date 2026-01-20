import json
import uuid
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate
from django.utils.decorators import method_decorator
from django.views import View
from .models import CustomUser


@method_decorator(csrf_exempt, name='dispatch')
class DesktopLoginView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            username = data.get('username')
            password = data.get('password')
            
            # 1. Authenticate standard credentials
            user = authenticate(username=username, password=password)
            
            if user is None:
                return JsonResponse({"error": "Invalid credentials"}, status=401)
            
            # 2. Check Subscription (The Gatekeeper)
            is_allowed = user.on_free_trial or user.has_active_subscription()

            if not is_allowed:
                return JsonResponse({
                    "error": "No active subscription found. Please upgrade on the website."
                }, status=403)
                
            # 3. Generate Session War Token
            new_session_id = str(uuid.uuid4())
            
            # 4. Save to DB (Invalidates any other running device immediately)
            user.desktop_session_id = new_session_id
            user.save(update_fields=['desktop_session_id'])
            
            return JsonResponse({
                "message": "Login successful",
                "session_id": new_session_id,
                "user_id": user.id,
                "username": user.username
            })
            
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


@method_decorator(csrf_exempt, name='dispatch')
class DesktopHeartbeatView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            user_id = data.get('user_id')
            client_session_id = data.get('session_id')
            
            # 1. Fast lookup
            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                return JsonResponse({"kill": True, "reason": "User not found"}, status=404)
            
            # 2. The Session War Logic
            # If the ID in DB is different from what client sent, 
            # it means a NEW login happened elsewhere.
            if user.desktop_session_id != client_session_id:
                return JsonResponse({
                    "kill": True, 
                    "reason": "Session expired. You are logged in on another device."
                }, status=403)
            
            # 3. Check subscription status again (In case it expired while app was running)
            is_allowed = user.on_free_trial or user.has_active_subscription()
            if not is_allowed:
                return JsonResponse({
                    "kill": True, 
                    "reason": "Trial or Subscription expired."
                }, status=403)

            return JsonResponse({"kill": False})
            
        except Exception as e:
            # If something breaks, don't kill the app, just warn
            return JsonResponse({"error": str(e)}, status=500)
        


@method_decorator(csrf_exempt, name='dispatch')
class ValidateScrapeRequestView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            user_id = data.get('user_id')
            requested_count = int(data.get('count', 0))
            
            user = CustomUser.objects.get(id=user_id)
            
            # 1. Unlimited Users
            if user.is_superuser or user.has_active_subscription():
                return JsonResponse({"allowed": True})

            # 2. Trial Users
            if user.on_free_trial:
                limit = 5000
                remaining = limit - user.trial_usage_count
                
                if requested_count > remaining:
                    return JsonResponse({
                        "allowed": False, 
                        "error": f"Insufficient tokens. You have {remaining} left, but requested {requested_count}."
                    }, status=403)
                
                # Deduct upfront
                user.trial_usage_count += requested_count
                user.save(update_fields=['trial_usage_count'])
                
                return JsonResponse({
                    "allowed": True, 
                    "remaining_tokens": limit - user.trial_usage_count
                })

            return JsonResponse({"allowed": False, "error": "No active trial or subscription."}, status=403)
            
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

