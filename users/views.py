from django.shortcuts import render, redirect
from django.contrib.auth import login
from .forms import CustomUserSignupForm
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy
from django.contrib import messages



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


