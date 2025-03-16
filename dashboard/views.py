from django.shortcuts import render

def index(request):
	return render(request, 'dashboard/index.html')


def campaign(request):
	return render(request, 'dashboard/campaign.html')


def email_account(request):
	return render(request, 'dashboard/email_account.html')



