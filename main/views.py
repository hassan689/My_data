from concurrent.futures import ThreadPoolExecutor
from django.core.mail import EmailMessage
from django.shortcuts import render, redirect
from django.conf import settings
from django.contrib import messages
from .forms import ContactForm, PaymentVerificationForm, RequestReceipt


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


def send_email_with_attachment(subject, message, recipient, file):
    """Sends an email with an image attachment."""
    try:
        email_message = EmailMessage(
            subject=subject,
            body=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient]
        )

        # Read the file before passing to EmailMessage
        if file:
            file_content = file.read()
            email_message.attach(file.name, file_content, file.content_type)

        email_message.send()
    except Exception as e:
        print(f"Email sending failed: {str(e)}")


def price_page(request):

    form = RequestReceipt()
    error_message = None  # To store error messages

    if request.method == "POST":
        form = RequestReceipt(request.POST)
        if form.is_valid():
            try:
                name = form.cleaned_data['name']
                email = form.cleaned_data['email']
                company_name = form.cleaned_data['company_name']

                subject = f"Receipt Requet from {name} at DispatchSkool"
                body = f"Name: {name}\nEmail: {email}\nComapny name:{company_name}"
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
        else:
            form = RequestReceipt()
            return redirect(request.path)

    return render(request, 'main/price_page.html', {'form': form})


def privacy_policy(request):
    return render(request, 'main/privacy_policy.html')

def terms_of_service(request):
    return render(request, 'main/terms_of_service.html')


# def price_page(request):
#     if request.method == 'POST':
#         form = PaymentVerificationForm(request.POST, request.FILES)
#         if form.is_valid():
#             # Extract data
#             full_name = form.cleaned_data['full_name']
#             email = form.cleaned_data['email']
#             payment_amount = form.cleaned_data['payment_amount']
#             payment_reference = form.cleaned_data['payment_reference']
#             receipt = request.FILES['file_upload']

#             # Email details
#             subject = "New Payment Verification Submission"
#             message = (
#                 f"Full Name: {full_name}\n"
#                 f"Email: {email}\n"
#                 f"Payment Amount: ${payment_amount}\n"
#                 f"Payment Reference: {payment_reference}\n\n"
#                 "Please find the attached receipt for verification."
#             )

#             # Send email
#             email_message = EmailMessage(
#                 subject,
#                 message,
#                 settings.DEFAULT_FROM_EMAIL,
#                 [settings.EMAIL_HOST_USER]  # Send to email host user
#             )

#             executor.submit(send_email_with_attachment, subject, message, settings.EMAIL_HOST_USER, receipt)
#             messages.success(request, "Your payment verification request has been submitted successfully!")
#             return redirect('main:index')  # Redirect to a success page

#     else:
#         form = PaymentVerificationForm()
    
#     return render(request, 'main/price_page.html', {'form': form})

