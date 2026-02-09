from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from .forms import SkipListForm
from .models import SkipList
from django.http import HttpResponse
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from django.contrib.auth import get_user_model

User = get_user_model()

@login_required
def skip_list_page(request):
    """
    Manages the SkipList for the logged-in user.

    - On GET, displays empty inputs for adding new entries and
      lists all existing entries.
    - On POST, adds new entries from the form to the user's
      existing skip list.
    """
    
    skip_list, created = SkipList.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        # 2. Bind the POST data AND the specific skip_list instance
        #    to the form.
        form = SkipListForm(request.POST, instance=skip_list)
        
        if form.is_valid():
            # 3. get the new items, append them to the
            #    'skip_list' instance's items, and remove duplicates.
            form.save()
            return redirect('leads_data:skip_list_page') 

    else:
        # 5. On a GET request, create a form bound to the instance.
        #    the inputs will render empty (showing placeholders).
        form = SkipListForm(instance=skip_list)
    
    # Get the currently saved lists from the object
    emails_list = skip_list.emails or []
    mcs_list = skip_list.mc_numbers or []

    context = {
        'form': form,
        'emails': emails_list,
        'mcs': mcs_list,
        'email_count': len(emails_list),
        'mc_count': len(mcs_list)
    }
    
    return render(request, 'leads_data/skip_lists.html', context)


def unsubscribe_view(request, token):
    signer = TimestampSigner()
    
    try:
        # 1. Decode the token 
        # max_age=5184000 allows the link to work for 60 days.
        data = signer.unsign_object(token, max_age=5184000)
        
        user_id = data.get('uid')
        email_to_remove = data.get('email')
        
        if not user_id or not email_to_remove:
            return HttpResponse("Invalid link data.", status=400)
        
        # 2. Find the User (Sender)
        user = User.objects.get(id=user_id)
        
        # 3. Get or Create the SkipList
        skip_list, created = SkipList.objects.get_or_create(user=user)
        
        # 4. Update the JSON List
        current_emails = skip_list.emails if isinstance(skip_list.emails, list) else []
        
        if email_to_remove not in current_emails:
            current_emails.append(email_to_remove)
            skip_list.emails = current_emails
            skip_list.save()
            
        # 5. Return Success Page (Simple HTML)
        return HttpResponse("""
            <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
                <h1 style="color: #333;">Unsubscribed</h1>
                <p style="color: #666;">You have been successfully removed from this mailing list.</p>
            </div>
        """)

    except (BadSignature, SignatureExpired):
        return HttpResponse("This unsubscribe link is invalid or has expired.", status=400)
    except User.DoesNotExist:
        return HttpResponse("The sender account for this link no longer exists.", status=404)


