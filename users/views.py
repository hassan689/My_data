from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, get_backends
import dns.resolver
import requests

from growth_skool import settings
# from .forms import CustomUserSignupForm, EmailLoginForm, OTPForm
from .forms import CustomUserSignupForm, AccountGroupForm, EmailAccountAssignmentFormSet, UserProfileUpdateForm
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy
from .models import AccountGroup, CustomUser, EmailAccount
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.contrib.auth.decorators import login_required
from django.contrib import messages
# import random
# from django.contrib.auth import get_user_model
# from .models import OTP
# from django.core.mail import send_mail
# from django.conf import settings
# from django.urls import reverse
# from django.http import HttpResponseBadRequest


# Uncomment the following code to enable email + OTP login functionality for future use. Not required now as. Business decision.

# def generate_otp():
#     """Generates a random 6-digit OTP."""
#     return str(random.randint(100000, 999999))


# def email_login_view(request):
    
#     form = EmailLoginForm()
#     if request.method == 'POST':
#         form = EmailLoginForm(request.POST)
#         if form.is_valid():
#             email = form.cleaned_data['email']
#             password = form.cleaned_data['password']

#             # Step 1: Attempt to authenticate the user with email and password
#             user = authenticate(request, email=email, password=password)

#             if user is not None:
#                 # Step 2: Authentication successful, proceed to OTP
#                 otp_code = generate_otp()

#                 try: 
#                     # Send the email with the OTP
#                     send = send_mail(
#                         'Your Login OTP',
#                         f'Your One-Time Password is: {otp_code}',
#                         settings.EMAIL_HOST_USER,
#                         [email],
#                         fail_silently=False,
#                     )
#                     print(send)
#                     print(f"Sent OTP {otp_code} to {email}")

#                     OTP.objects.filter(user=user, is_used=False).delete()

#                     otp_instance = OTP(user=user)
#                     otp_instance.set_code(otp_code)
#                     otp_instance.save()

#                 except Exception as e:
#                     messages.error(request, "Failed Action. Please retry!")
#                     return render(request, 'registration/email_login.html', {'form': form})

#                 # Only redirect if OTP was sent successfully
#                 return redirect(reverse('users:otp_verify') + f'?email={email}')
#             else:
#                 messages.error(request, "Invalid email or password.")
#                 return render(request, 'registration/email_login.html', {'form': form})
#         else:
#             # Form is invalid, re-render with errors
#             return render(request, 'registration/email_login.html', {'form': form})
#     else:
#         form = EmailLoginForm()
#     return render(request, 'registration/email_login.html', {'form': form})


# def otp_verification_view(request):
#     email = request.GET.get('email')
#     if not email:
#         return HttpResponseBadRequest("Email parameter is missing.")

#     if request.method == 'POST':
#         form = OTPForm(request.POST)
#         if form.is_valid():
            
#             otp_code = form.cleaned_data['otp_code'].strip()
#             User = get_user_model()
#             try:
#                 user = User.objects.get(email=email)
                
#                 # Find the most recent, unused OTP for this user
#                 otp_instance = OTP.objects.filter(user=user, is_used=False).order_by('-created_at').first()

#                 if otp_instance and otp_instance.is_valid() and otp_instance.check_code(otp_code):
#                     otp_instance.is_used = True
#                     otp_instance.save()

#                     backend = get_backends()[0]  # use the first backend
#                     user.backend = f"{backend.__module__}.{backend.__class__.__name__}"

#                     # Log the user in
#                     login(request, user)
#                     return redirect('dashboard:index')  # Redirect to your home page
#                 else:
#                     messages.error(request, "Invalid or expired OTP.")
#             except User.DoesNotExist:
#                 messages.error(request, "User does not exist.")
#     else:
#         form = OTPForm()

#     return render(request, 'registration/otp_verify.html', {'form': form, 'email': email})



def signup_view(request):
    
    form = CustomUserSignupForm()
    if request.method == "POST":
        form = CustomUserSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Signup successful! Welcome aboard.")
            return redirect("dashboard:index")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.capitalize()}: {error}")
    else:
        form = CustomUserSignupForm()

    return render(request, "registration/signup.html", {"form": form})


class CustomPasswordResetView(auth_views.PasswordResetView):
    """Custom Password Reset View to explicitly define success URL and pass request to template."""
    
    success_url = reverse_lazy("users:password_reset_done")  # Explicitly set the success URL

    def get_context_data(self, **kwargs):
        """Ensure request is included in the email template context."""
        context = super().get_context_data(**kwargs)
        context["request"] = self.request  # Fix the missing request issue
        return context

class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Custom Password Reset Confirm View to ensure correct success URL and pass request to template."""
    
    success_url = reverse_lazy("users:password_reset_complete")  # ✅ Use namespaced success URL

    def get_context_data(self, **kwargs):
        """Ensure request is included in the template context to avoid VariableDoesNotExist errors."""
        context = super().get_context_data(**kwargs)
        context["request"] = self.request  # ✅ Fix for missing request in email template
        return context



# @login_required
# @require_http_methods(["GET", "POST"])
# def account_groups(request):
    
#     if request.method == 'POST':
#         form = AccountGroupForm(request.POST, user=request.user)
        
#         if form.is_valid():
#             form.save()
#             messages.success(request, "New Group created successfully.")

#             # Redirect back to this same view to show the message
#             return redirect('dashboard:account_groups')
#     else:
#         # On a GET request, show an empty form
#         form = AccountGroupForm(user=request.user)

#     account_groups = AccountGroup.objects.filter(user=request.user)
#     email_accounts = EmailAccount.objects.filter(user=request.user)

#     # We use a new template name, as this page now does more than just show a form
#     return render(request, 'dashboard/account_groups.html', {
#         'form': form,
#         'account_groups': account_groups,
#         'count': len(account_groups),
#         'email_accounts': email_accounts,
#     })

@login_required
@require_http_methods(["GET", "POST"])
def account_groups(request):
    
    # Get the queryset for the formset
    email_accounts_qs = EmailAccount.objects.filter(user=request.user)

    # Initialize both forms
    group_form = AccountGroupForm(user=request.user)
    assignment_formset = EmailAccountAssignmentFormSet(
        queryset=email_accounts_qs,
        prefix='assignments' # Use a prefix to avoid field name collisions
    )

    if request.method == 'POST':
        # Check which button was clicked
        
        if 'create_group' in request.POST:
            # --- Handle the Create Group form ---
            group_form = AccountGroupForm(request.POST, user=request.user)
            if group_form.is_valid():
                group_form.save()
                messages.success(request, "New Group created successfully.")
                return redirect('dashboard:account_groups')

        elif 'save_assignments' in request.POST:
            # --- Handle the Assignments FormSet ---
            assignment_formset = EmailAccountAssignmentFormSet(
                request.POST, 
                queryset=email_accounts_qs, 
                prefix='assignments'
            )
            if assignment_formset.is_valid():
                assignment_formset.save()
                messages.success(request, "Group assignments saved successfully.")
                return redirect('dashboard:account_groups')
            else:
                messages.error(request, "Please correct the assignment errors below.")

    # On GET request (or if a form was invalid), render the page
    account_groups = AccountGroup.objects.filter(user=request.user)

    return render(request, 'dashboard/account_groups.html', {
        'form': group_form,  # This is the AccountGroupForm
        'assignment_formset': assignment_formset, # This is the new FormSet
        'account_groups': account_groups,
        'count': account_groups.count(),
    })


@login_required
@require_POST  # Ensures this view can only be accessed via POST
def delete_group(request, group_id):
    group = get_object_or_404(AccountGroup, id=group_id, user=request.user)
    group_name = group.name
    
    # Delete the object
    group.delete()
    messages.success(request, f"Group '{group_name}' has been successfully deleted.")
    return redirect('dashboard:account_groups')



@login_required
def user_profile_view(request):
    user = request.user
    
    if request.method == 'POST':
        form = UserProfileUpdateForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect('users:user_profile_edit')
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = UserProfileUpdateForm(instance=user)

    context = {
        'form': form,
        'is_verified': user.tracking_domain_verified,
        'tracking_domain': user.tracking_custom_domain,
    }
    return render(request, 'registration/profile_edit.html', context)


@require_GET
def check_domain(request):
    """
    Unified SSL validation endpoint for Caddy.
    Bcz Caddy file can have only 1 "ask" directive, we need to check both DispatchSkool and ColdSkool in the same view.

    Logic:
    1. Check DispatchSkool DB
    2. If not found, ask ColdSkool
    3. Allow if either approves
    """

    domain = request.GET.get('domain')
    if not domain:
        return HttpResponse('Domain required', status=400)

    domain = domain.lower().strip()

    # 1. DispatchSkool DB check
    if domain in getattr(settings, 'SYSTEM_DOMAINS', []):
        return HttpResponse('OK')

    if CustomUser.objects.filter(
        tracking_custom_domain=domain,
        tracking_domain_verified=True
    ).exists():
        return HttpResponse('OK')

    if EmailAccount.objects.filter(
        tracking_custom_domain=domain,
        tracking_domain_verified=True
    ).exists():
        return HttpResponse('OK')

    # 2. Fallback: ask ColdSkool
    try:
        resp = requests.get(
            "https://coldskool.com/check-domain/",
            params={"domain": domain},
            timeout=1.5
        )

        if resp.status_code == 200:
            return HttpResponse('OK')

    except requests.RequestException:
        pass

    # 3. Deny the incoming domain, if both checks failed
    return HttpResponse('Unauthorized', status=400)


@login_required
@require_POST
def verify_tracking_dns(request):
    user = request.user
    domain = user.tracking_custom_domain

    if not domain:
        return JsonResponse({'success': False, 'error': 'No domain saved to verify.'})

    # The target they must point to (Server)
    REQUIRED_TARGET = "whitelabel.dispatchskool.com."
    
    try:
        # Perform the DNS lookup
        answers = dns.resolver.resolve(domain, 'CNAME')
        
        for rdata in answers:
            # DNS targets usually end with a dot (e.g., target.com.)
            target = rdata.target.to_text()
            
            # Compare (handling the potential missing/present trailing dot)
            if target.rstrip('.') == REQUIRED_TARGET.rstrip('.'):
                
                # SUCCESS: Pointing to us
                user.tracking_domain_verified = True
                user.save(update_fields=['tracking_domain_verified'])
                return JsonResponse({'success': True, 'message': 'Domain verified successfully!'})

        # If we loop through and don't find the match
        return JsonResponse({
            'success': False, 
            'error': f'CNAME record found, but it points to {target}, not {REQUIRED_TARGET}'
        })

    except dns.resolver.NoAnswer:
        return JsonResponse({'success': False, 'error': 'No CNAME record found. Please add it in your DNS settings.'})
        
    except dns.resolver.NXDOMAIN:
        return JsonResponse({'success': False, 'error': 'Domain does not exist.'})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'DNS Lookup failed: {str(e)}'})


