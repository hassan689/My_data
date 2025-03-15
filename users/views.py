from django.shortcuts import render, redirect
from django.contrib.auth import login
from .forms import CustomUserSignupForm
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy


def signup_view(request):
    if request.method == "POST":
        form = CustomUserSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)  # Auto-login after signup
            return redirect("main:index")  # Redirect to dashboard or any other page
    else:
        form = CustomUserSignupForm()
    
    return render(request, "registration/signup.html", {"form": form})


class CustomPasswordResetView(auth_views.PasswordResetView):
    """Custom Password Reset View to explicitly define success URL."""
    success_url = reverse_lazy("users:password_reset_done")  # Explicitly set the success URL


