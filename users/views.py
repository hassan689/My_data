from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate
from .forms import CustomUserSignupForm, EmailLoginForm, OTPForm
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy
from django.contrib import messages
import random
from django.contrib.auth import get_user_model
from .models import OTP
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from django.http import HttpResponseBadRequest


def generate_otp():
    """Generates a random 6-digit OTP."""
    return str(random.randint(100000, 999999))


def email_login_view(request):
    
    form = EmailLoginForm()
    if request.method == 'POST':
        form = EmailLoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']

            # Step 1: Attempt to authenticate the user with email and password
            user = authenticate(request, email=email, password=password)

            if user is not None:
                # Step 2: Authentication successful, proceed to OTP
                otp_code = generate_otp()

                try: 
                    # Send the email with the OTP
                    send_mail(
                        'Your Login OTP',
                        f'Your One-Time Password is: {otp_code}',
                        settings.DEFAULT_FROM_EMAIL,
                        [email],
                        fail_silently=False,
                    )
                    OTP.objects.filter(user=user, is_used=False).delete()

                    otp_instance = OTP(user=user)
                    otp_instance.set_code(otp_code)
                    otp_instance.save()

                except:
                    messages.error(request, "Failed Action. Please retry!")
                
                # Redirect to the OTP verification page
                return redirect(reverse('users:otp_verify') + f'?email={email}')
            else:
                messages.error(request, "Invalid email or password.")
                # The view will fall through and re-render the form with the error message
    else:
        form = EmailLoginForm()
        
    return render(request, 'login/email_login.html', {'form': form})


def otp_verification_view(request):
    email = request.GET.get('email')
    if not email:
        return HttpResponseBadRequest("Email parameter is missing.")

    if request.method == 'POST':
        form = OTPForm(request.POST)
        if form.is_valid():
            
            otp_code = form.cleaned_data['otp_code'].strip()
            User = get_user_model()
            try:
                user = User.objects.get(email=email)
                
                # Find the most recent, unused OTP for this user
                otp_instance = OTP.objects.filter(user=user, is_used=False).order_by('-created_at').first()

                if otp_instance and otp_instance.is_valid() and otp_instance.check_code(otp_code):
                    otp_instance.is_used = True
                    otp_instance.save()
                    # Log the user in
                    login(request, user)
                    return redirect('home')  # Redirect to your home page
                else:
                    messages.error(request, "Invalid or expired OTP.")
            except User.DoesNotExist:
                messages.error(request, "User does not exist.")
    else:
        form = OTPForm()

    return render(request, 'login/otp_verify.html', {'form': form, 'email': email})



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


