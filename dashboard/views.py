from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from users.models import EmailAccount
from .forms import EmailAccountForm


@login_required
def index(request):
	email_accounts = EmailAccount.objects.filter(user=request.user, is_active=True)
	context = {
		"email_accounts": email_accounts
	}
	return render(request, 'dashboard/index.html', context)

@login_required
def campaign(request):
	return render(request, 'dashboard/campaign.html')


@login_required
def add_email_account(request):
    
    form = EmailAccountForm()
    if request.method == "POST":
        form = EmailAccountForm(request.POST)
        if form.is_valid():
            email_account = form.save(commit=False)
            email_account.user = request.user  # Assign authenticated user
            email_account.save()
            return redirect("dashboard:index")
    else:
        form = EmailAccountForm()
    context = {
        "form": form
    }
    return render(request, 'dashboard/add_email_account.html', context)


# Update Email Account
@login_required
def email_account_update(request, id):
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    form = EmailAccountForm(instance=email_account)
    
    if request.method == "POST":
        form = EmailAccountForm(request.POST, instance=email_account)
        if form.is_valid():
            form.save()
            return redirect("dashboard:index")
    else:
        form = EmailAccountForm(instance=email_account)
    
    return render(request, "dashboard/add_email_account.html", {"form": form})

# Soft Delete (Deactivate)
@login_required
def email_account_delete(request, id):
    
    email_account = get_object_or_404(EmailAccount, id=id, user=request.user)
    email_account.delete()
    return redirect("dashboard:index")

