from concurrent.futures import ThreadPoolExecutor
from django.core.mail import EmailMessage
from django.shortcuts import render, redirect
from django.conf import settings
from .forms import ContactForm

executor = ThreadPoolExecutor(max_workers=5)  # Thread pool for async execution

def send_email_async(email_message):
    """Function to send an email asynchronously."""
    try:
        email_message.send()
    except Exception as e:
        print(f"Email sending failed: {str(e)}")

def index(request):
    form = ContactForm()
    error_message = None  # To store error messages

    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            try:
                name = form.cleaned_data['name']
                email = form.cleaned_data['email']
                message = form.cleaned_data['message']

                subject = f"New Contact Form Submission from {name}"
                body = f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
                from_email = settings.EMAIL_HOST_USER
                recipient_list = [settings.EMAIL_HOST_USER]

                email_message = EmailMessage(
                    subject,
                    body,
                    from_email,
                    recipient_list,
                    reply_to=[email],
                )

                # Submit the email sending task to the executor
                executor.submit(send_email_async, email_message)

                return redirect('main:index')  # Redirect on success
            except Exception as e:
                error_message = f"An error occurred: {str(e)}"

    return render(request, 'main/index.html', {'form': form, 'error_message': error_message})
