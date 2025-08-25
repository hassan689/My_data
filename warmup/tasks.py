import random
import re
from django.utils import timezone
from .models import WarmupCampaign, WarmupTemplateSet, WarmupMessage
from users.models import EmailAccount
from growth_skool.celery import app
from django.core.mail import get_connection, EmailMultiAlternatives, send_mail, EmailMessage
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_str
import time


@app.task(name="warmup.tasks.activate_warmup_campaign")
def activate_warmup_campaign(sender_account_id, template_set_id):
    
    sender_account = EmailAccount.objects.get(id=sender_account_id)
    template_set = WarmupTemplateSet.objects.get(id=template_set_id)

    # Select 10 random targets, 2 for localhost testing accounts
    target_accounts = list(EmailAccount.objects.filter(is_warmup_target=True).exclude(id=sender_account_id).order_by('?')[:2])
    
    if not target_accounts:
        print("Not enough warmup target accounts available.")
        return

    # Create the WarmupCampaign instance
    campaign = WarmupCampaign.objects.create(
        sender_account=sender_account,
        template_set=template_set,
        status='Active',
        last_action_at=timezone.now(),
        next_action_at = timezone.now() + timedelta(minutes=random.uniform(2, 5)) # for testing on localhost
        # next_action_at=timezone.now() + timedelta(hours=random.uniform(24, 36))  # Random delay 24-36 hours
    )
    campaign.target_accounts.set(target_accounts)

    # Trigger the first step of the conversation
    send_warmup_step.delay(campaign.id, step_number=0)

    print(f"\nWarmup campaign for {sender_account.email_address} activated.\n")



def personalize_template(template, lead):
    
    # Find all placeholders like [Some Column]
    placeholders = re.findall(r'\[([^\]]+)\]', template)
    
    for ph in placeholders:
        value = str(lead.get(ph, ph))  # Use the column name as fallback if missing
        template = template.replace(f"[{ph}]", value)
    
    return template



def get_email_connection(email_account, decrypted_password):
    """
    Establishes and opens an SMTP connection for sending emails.
    """
    use_tls = email_account.server_type == "STARTTLS" or email_account.server_type == "TLS"
    use_ssl = email_account.server_type == "SSL"

    if use_tls and use_ssl:
        print("Invalid configuration: Cannot enable all TLS, SSL and STARTTLS.")
        return

    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=email_account.host,
        port=email_account.port_number,
        username=email_account.email_address,
        password=decrypted_password,
        use_tls=use_tls,
        use_ssl=use_ssl,
    )
    connection.open()
    return connection


@app.task(name="warmup.tasks.send_warmup_step")
def send_warmup_step(campaign_id, step_number):
    """
    Sends the next step of a warmup conversation for a given campaign.
    Even steps (0, 2, 4...) are for the sender, odd steps (1, 3, 5...) are for the targets.
    """
    try:
        campaign = WarmupCampaign.objects.select_related(
            'sender_account', 'template_set'
        ).prefetch_related(
            'target_accounts'
        ).get(id=campaign_id)
    except WarmupCampaign.DoesNotExist:
        print(f"Warmup campaign with ID {campaign_id} not found.")
        return

    # Check if the campaign has already completed or failed
    if campaign.status in ['Complete', 'Failed']:
        print(f"Campaign {campaign.id} is already in {campaign.status} state. Skipping.")
        return

    # Handle missing templates with its own try-except block
    try:
        templates = campaign.template_set.templates.get(f'step_{step_number + 1}')
    except Exception as e:
        subject = f"Template not found. Ending warmup campaign."
        body = f"Template for step {step_number + 1} not found. Ending campaign for {campaign.sender_account}: {e}"
        recipient_list = ['abdullahatif132@gmail.com']
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            recipient_list,
            fail_silently=False,
        )
        print(f"Template for step {step_number + 1} not found. Ending campaign.")
        campaign.status = 'Failed'
        campaign.save(update_fields=['status'])
        return
    
    # Sender's turn (Even steps: 0, 2, 4...)
    if step_number % 2 == 0:
        try:
            sender_account = campaign.sender_account
            recipients = list(campaign.target_accounts.all())

            # Establish connection for the sender account
            decrypted_password = sender_account.get_password()
            connection = get_email_connection(sender_account, decrypted_password)
            
            for recipient_account in recipients:
                template = random.choice(templates)
                personalization_data = {
                    'first_name': recipient_account.user.first_name,
                    'email': recipient_account.email_address,
                    'company_name': getattr(recipient_account.user, "company_name", "ABC Transports LLC"),
                    'topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'specific_task': 'Auto-Warmup',
                    'our_process': 'Automation',
                    'new_topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'our_strategy': 'Automation',
                    'final_topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'our_final_strategy': 'Automation',
                }

                appended_message = """\n\n\n
                This email is part of the Dispatch Skool Auto-Warmup Campaign.
                The purpose of this campaign is to warm up your email sending reputation.
                We do this by sending and receiving messages between a private pool of verified accounts.
                This activity mimics natural human conversation, which helps major email providers like Google
                and Microsoft see your account as trustworthy. By participating, your account's deliverability will improve,
                ensuring your legitimate emails reach their intended recipients rather than landing in spam folders.\n

                Regards,
                The Dispatch Skool Team
                """
                
                def clean_text(text: str) -> str:
                    return text.replace('\xa0', ' ').encode('utf-8', 'ignore').decode('utf-8')

                personalized_subject = clean_text(personalize_template(template['subject'], personalization_data))
                personalized_body = clean_text(personalize_template(template['body'], personalization_data))
                
                main_msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=sender_account.email_address,
                    to=[recipient_account.email_address],
                    connection=connection
                )
                main_msg.encoding = 'utf-8'
                try:
                    main_msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower() or "Connection unexpectedly closed" in str(e).lower() or "Connection reset by peer" in str(e).lower():
                        connection = get_email_connection(sender_account, decrypted_password)
                        main_msg.connection = connection
                        main_msg.send()
                    else:
                        raise e

                WarmupMessage.objects.create(
                    campaign=campaign,
                    sender=sender_account,
                    recipient=recipient_account,
                    subject=personalized_subject,
                    body=personalized_body
                )

                time.sleep(random.randint(30, 60))

            if connection:
                connection.close()

        except Exception as e:
            
            if "Username and Password not accepted" in str(e):

                campaign.status = 'Failed'
                campaign.save(update_fields=['status'])

                sender_account.is_warmup_target = False # the account is not attached properly and will only be a pain to keep attempting the  warmup
                sender_account.save(update_fields=['is_warmup_target'])

                subject = "Email account configuration failure"
                body = (
                    f"Hello {sender_account.user.first_name},\n\n"
                    f"Error during email attach: {e}\n\n"
                    f"This is to notify you that your email account {sender_account.email_address} "
                    "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                    "In case of any problems, feel free to reach out.\n\n"
                    "Best Regards,\nThe Dispatch Skool Team."
                )
                from_email = settings.EMAIL_HOST_USER
                recipient_list = [sender_account.user.email]

                body_encoded = force_str(body, 'utf-8', errors='replace')

                email_message = EmailMessage(
                    subject,
                    body_encoded,
                    from_email,
                    recipient_list
                )
                email_message.send()
            
            elif "Daily user sending limit exceeded" in str(e):
                
                # This just means that campaign can't be sent today, so it'll just be set to a later date
                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                campaign.save(update_fields=['next_action_at'])

            elif "Connection unexpectedly closed" in str(e):
                
                connection = get_email_connection(sender_account, decrypted_password)
                try:
                    main_msg.connection = connection
                    main_msg.send()
                except: # retry on a later date
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                    campaign.save(update_fields=['next_action_at'])

            else: # if something other than the currently known errors, then tell me those
                subject = f"Error during Sender's Turn for Warmup Campaign"
                body = f"Error during sender's turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                recipient_list = ['abdullahatif132@gmail.com']
                send_mail(
                    subject,
                    body,
                    settings.DEFAULT_FROM_EMAIL,
                    recipient_list,
                    fail_silently=False,
                )
                campaign.status = 'Failed'
                campaign.save(update_fields=['status'])

            return
    
    # Targets' turn (Odd steps: 1, 3, 5...)
    else:
        sender_accounts = list(campaign.target_accounts.all())
        recipient_account = campaign.sender_account
        
        for sender_account in sender_accounts:
            
            try: # try for every account and handle thier individual errors accordingly without halting the campaign as much as possible
                decrypted_password = sender_account.get_password()
                connection = get_email_connection(sender_account, decrypted_password)
                
                template = random.choice(templates)
                personalization_data = {
                    'first_name': recipient_account.user.first_name,
                    'email': recipient_account.email_address,
                    'company_name': getattr(recipient_account.user, "company_name", "ABC Transports LLC"),
                    'topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'specific_task': 'Auto-Warmup',
                    'our_process': 'Automation',
                    'new_topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'our_strategy': 'Automation',
                    'final_topic': 'Dispatch Skool Auto-Warmup Campaign',
                    'our_final_strategy': 'Automation',
                }
                
                appended_message = """\n\n\n
                This email is part of the Dispatch Skool Auto-Warmup Campaign.
                The purpose of this campaign is to warm up your email sending reputation.
                We do this by sending and receiving messages between a private pool of verified accounts.
                This activity mimics natural human conversation, which helps major email providers like Google
                and Microsoft see your account as trustworthy. By participating, your account's deliverability will improve,
                ensuring your legitimate emails reach their intended recipients rather than landing in spam folders.\n

                Regards,
                The Dispatch Skool Team
                """
                
                def clean_text(text: str) -> str:
                    return text.replace('\xa0', ' ').encode('utf-8', 'ignore').decode('utf-8')

                personalized_subject = clean_text(personalize_template(template['subject'], personalization_data))
                personalized_body = clean_text(personalize_template(template['body'], personalization_data))
                
                main_msg = EmailMultiAlternatives(
                    subject=personalized_subject,
                    body=personalized_body,
                    from_email=sender_account.email_address,
                    to=[recipient_account.email_address],
                    connection=connection
                )
                main_msg.encoding = 'utf-8'
                try:
                    main_msg.send()
                except Exception as e:
                    if "please run connect() first" in str(e).lower() or "connection expired" in str(e).lower() or "Connection unexpectedly closed" in str(e).lower() or "Connection reset by peer" in str(e).lower() or "Disabled by user from hPanel" in str(e).lower():
                        print("SMTP connection lost, reconnecting...")
                        connection = get_email_connection(sender_account, decrypted_password)
                        main_msg.connection = connection
                        main_msg.send()
                    else:
                        raise e
                    
                time.sleep(random.randint(30, 600))

                WarmupMessage.objects.create(
                    campaign=campaign,
                    sender=sender_account,
                    recipient=recipient_account,
                    subject=personalized_subject,
                    body=personalized_body,
                )

                if connection:
                    connection.close()

            except Exception as e:
                
                if "Connection reset by peer" in str(e) or "Disabled by user from hPanel" in str(e):
            
                    connection = get_email_connection(sender_account, decrypted_password)
                    try:
                        main_msg.connection = connection
                        main_msg.send()
                    except:
                        continue
                    
                elif "Daily user sending limit exceeded" in str(e):
                
                    # Using continue bcz, there might be only some whose daily limit is reached and not all, so those accounts will simple be skipped
                    continue

                elif "Username and Password not accepted" in str(e):

                    sender_account.is_warmup_target = False # the account is not attached properly and will only be a pain to keep attempting the  warmup
                    sender_account.save(update_fields=['is_warmup_target'])

                    subject = "Email account configuration failure"
                    body = (
                        f"Hello {sender_account.user.first_name},\n\n"
                        f"Error during email attach: {e}\n\n"
                        f"This is to notify you that your email account {sender_account.email_address} "
                        "could not be configured with Dispatch Skool. This is likely due to incorrect credentials entered. Please refer to the provided instructions on the add account page and try 'updating' the account you were trying to attach.\n\n"
                        "In case of any problems, feel free to reach out.\n\n"
                        "Best Regards,\nThe Dispatch Skool Team."
                    )
                    from_email = settings.EMAIL_HOST_USER
                    recipient_list = [sender_account.user.email]

                    body_encoded = force_str(body, 'utf-8', errors='replace')

                    email_message = EmailMessage(
                        subject,
                        body_encoded,
                        from_email,
                        recipient_list
                    )
                    email_message.send()

                    continue

                else: # if something other than the currently known errors, then tell me those
                    subject = f"Error during Targets' Turn for Warmup Campaign"
                    body = f"Error during targets' turn (step {step_number}) for Campaign sender {campaign.sender_account}: {e}"
                    recipient_list = ['abdullahatif132@gmail.com']
                    send_mail(
                        subject,
                        body,
                        settings.DEFAULT_FROM_EMAIL,
                        recipient_list,
                        fail_silently=False,
                    )
                    continue

    # Update campaign status for the next step (only runs if no errors occurred)
    campaign.current_step += 1
    campaign.last_action_at = timezone.now()
    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
    if campaign.current_step >= 10:
        campaign.status = 'Complete'
    campaign.save(update_fields=['current_step', 'last_action_at', 'next_action_at', 'status'])
    
    print(f"Warmup campaign {campaign.id} processed step {step_number} and is now at step {campaign.current_step}.")


@app.task(name="warmup.tasks.process_warmup_convo_beats")
def process_warmup_convo_beats():
    """
    Celery Beat task that checks for active warmup campaigns due for their next step.
    """
    now_utc = timezone.now()
    
    # Find active campaigns where next_action_at is due or last_action_at is null (initial step)
    campaigns_to_process = WarmupCampaign.objects.filter(
        status='Active',
        next_action_at__lte=now_utc
    ).select_related('sender_account', 'template_set')  # Optimize the query

    if not campaigns_to_process.exists():
        print("No warmup campaigns found to process.")
        return

    print(f"\nFound {campaigns_to_process.count()} warmup campaigns to process.\n")
    for campaign in campaigns_to_process:
        try:
            # Trigger the send_warmup_step task for the current step
            send_warmup_step.delay(campaign.id, campaign.current_step)
            print(f"\nTriggered send_warmup_step for WarmupCampaign {campaign.id}, Step {campaign.current_step}.\n")
            
        except Exception as e:
            # Log any errors that occur during the launching process
            print(f"Error triggering send_warmup_step for Campaign {campaign.id}: {e}")
            campaign.status = 'Failed'
            campaign.save(update_fields=['status'])

