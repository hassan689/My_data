import random
from django.utils import timezone
from .models import WarmupCampaign, WarmupMessage
from users.models import EmailAccount
from growth_skool.celery import app
from django.core.mail import EmailMultiAlternatives, send_mail, EmailMessage
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_str
import time
from django.db import connections
from celery.exceptions import SoftTimeLimitExceeded
from .utilities import *


@app.task(name="warmup.tasks.send_warmup_step", soft_time_limit=600, time_limit=700)
def send_warmup_step(campaign_id, step_number):
    """
    Sends the next step of a warmup conversation for a given campaign.
    Even steps (0, 2, 4...) are for the sender, odd steps (1, 3, 5...) are for the targets.
    """
    try:
    
        try:
            campaign = WarmupCampaign.objects.select_related(
                'sender_account'
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
        
        # NEW LOGIC: Refresh targets after a full cycle (e.g., every 2 steps)
        if step_number > 0 and step_number % 2 == 0:
            refresh_targets(campaign)
        
        # Sender's turn (Even steps: 0, 2, 4...)
        if step_number % 2 == 0:
            try:
                sender_account = campaign.sender_account
                recipients = list(campaign.target_accounts.all())

                # Establish connection for the sender account
                decrypted_password = sender_account.get_password()
                connection = get_email_connection(sender_account, decrypted_password)
                if not connection:
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                    campaign.save(update_fields=["next_action_at"])
                    return
                
                for recipient_account in recipients:

                    # NEW LOGIC: Generate subject and body dynamically
                    personalized_subject = generate_gibberish_subject()
                    personalized_body = generate_gibberish_body(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports LLC"))
                    
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
                        if "please run connect() first" in str(e) or "connection expired" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection reset by peer" in str(e):
                            connection = get_email_connection(sender_account, decrypted_password)
                            if not connection:
                                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                                campaign.save(update_fields=["next_action_at"])
                                return
                            main_msg.connection = connection
                            main_msg.send()
                        else:
                            raise e

                    # No need to save warmup msgs
                    # WarmupMessage.objects.create(
                    #     campaign=campaign,
                    #     sender=sender_account,
                    #     recipient=recipient_account,
                    #     subject=personalized_subject,
                    #     body=personalized_body
                    # )

                    connections.close_all()
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

                elif "codec can't encode character" in str(e): # '\xa0' error
                    
                    personalized_subject = generate_gibberish_subject()
                    personalized_body = generate_gibberish_body(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports LLC"))
                    
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
                        if "please run connect() first" in str(e) or "connection expired" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection reset by peer" in str(e):
                            connection = get_email_connection(sender_account, decrypted_password)
                            if not connection:
                                campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                                campaign.save(update_fields=["next_action_at"])
                                return
                            main_msg.connection = connection
                            main_msg.send()
                        else: # retry later
                            campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                            campaign.save(update_fields=['next_action_at'])

                elif "Connection unexpectedly closed" in str(e) or "too many AUTH commands" in str(e) or "Connection timed out" in str(e) or "Server busy" in str(e) or "Server not connected" in str(e) or "timeout exceeded" in str(e) or "Connection reset by peer" in str(e):
                    
                    connection = get_email_connection(sender_account, decrypted_password)
                    if not connection:
                        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                        campaign.save(update_fields=["next_action_at"])
                        return
                    try:
                        main_msg.connection = connection
                        main_msg.send()
                    except: # retry on a later date
                        campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(1, 3))
                        campaign.save(update_fields=['next_action_at'])

                elif "Temporary System Problem" in str(e) or "Concurrent connections limit exceeded" in str(e):
                    
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                    campaign.save(update_fields=['next_action_at'])

                elif "Please log in with your web browser" in str(e) or "Sender address rejected" in str(e): # These accounts will cause trouble for others as well
                    EmailAccount.objects.filter(email_address=sender_account.email_address).update(is_warmup_target=False, black_list=True)
                
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
                    campaign.next_action_at = timezone.now() + timedelta(hours=random.uniform(24, 36))
                    campaign.save(update_fields=['next_action_at'])

                return
        
        # Targets' turn (Odd steps: 1, 3, 5...)
        else:
            sender_accounts = list(campaign.target_accounts.all())
            recipient_account = campaign.sender_account
            
            for sender_account in sender_accounts:
                
                try: # try for every account and handle thier individual errors accordingly without halting the campaign as much as possible
                    decrypted_password = sender_account.get_password()
                    connection = get_email_connection(sender_account, decrypted_password)
                    if not connection:
                        continue
                    
                    personalized_subject = generate_gibberish_subject()
                    personalized_body = generate_gibberish_body(recipient_account.user.first_name, getattr(sender_account.user, "company_name", "ABC Transports LLC"))
                    
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
                        if "please run connect() first" in str(e) or "connection expired" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection reset by peer" in str(e) or "Disabled by user from hPanel" in str(e):
                            print("SMTP connection lost, reconnecting...")
                            connection = get_email_connection(sender_account, decrypted_password)
                            if not connection:
                                continue
                            main_msg.connection = connection
                            main_msg.send()
                        else:
                            raise e
                        
                    connections.close_all()
                    # time.sleep(random.randint(30, 600)) # No need to sleep during targets' turn, cz each sender here is different 

                    # WarmupMessage.objects.create(
                    #     campaign=campaign,
                    #     sender=sender_account,
                    #     recipient=recipient_account,
                    #     subject=personalized_subject,
                    #     body=personalized_body,
                    # )

                    if connection:
                        connection.close()

                except Exception as e:
                    
                    if "Connection reset by peer" in str(e) or "too many AUTH commands" in str(e) or "Disabled by user from hPanel" in str(e) or "Connection unexpectedly closed" in str(e) or "Connection timed out" in str(e):
                
                        connection = get_email_connection(sender_account, decrypted_password)
                        if not connection:
                            continue
                        try:
                            main_msg.connection = connection
                            main_msg.send()
                        except:
                            continue

                    elif "codec can't encode character" in str(e): # '\xa0' error
                        continue
                    
                    elif "Please log in with your web browser" in str(e) or "Sender address rejected" in str(e): # These accounts will cause trouble for others as well
                        EmailAccount.objects.filter(email_address=sender_account.email_address).update(is_warmup_target=False, black_list=True)
                        continue
                        
                    elif "Daily user sending limit exceeded" in str(e):
                    
                        # Using continue bcz, there might be only some whose daily limit is reached and not all, so those accounts will simple be skipped
                        continue

                    elif "Username and Password not accepted" in str(e) or "authentication failed" in str(e):

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
        
        campaign.save(update_fields=['current_step', 'last_action_at', 'next_action_at'])
        
        print(f"Warmup campaign {campaign.id} processed step {step_number} and is now at step {campaign.current_step}.")

    except SoftTimeLimitExceeded:
        print(f"[TIMEOUT] Warmup step {step_number} exceeded time limit — safely rescheduling.")
        WarmupCampaign.objects.filter(id=campaign_id).update(
            next_action_at=timezone.now() + timedelta(hours=random.uniform(1, 3))
        )
        connections.close_all()
        return


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


# beat task to clear out warmup messages older than 7 days
@app.task(name="warmup.tasks.clear_old_warmup_messages")
def clear_old_warmup_messages():
    cutoff_date = timezone.now() - timedelta(days=7)
    WarmupMessage.objects.filter(sent_at__lt=cutoff_date).delete()


