from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from .forms import SkipListForm
from .models import SkipList

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

