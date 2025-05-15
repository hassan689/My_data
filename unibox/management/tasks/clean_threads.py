from unibox.models import EmailThread
from dashboard.models import OutgoingEmailMessage


def cleanup_threads_with_no_message():
    threads_to_delete = []

    for thread in EmailThread.objects.all():
        messages = thread.get_ordered_messages()

        if len(messages) == 0:
            # No messages at all
            threads_to_delete.append(thread.id)

    # Bulk delete the filtered threads
    if threads_to_delete:
        deleted_count, _ = EmailThread.objects.filter(id__in=threads_to_delete).delete()
        print(f"🧹 Deleted {deleted_count} threads with 0 or only 1 outgoing message.")
    else:
        print("✅ No empty or single-outgoing-message threads found.")


def cleanup_threads_with_only_outgoing_message():
    threads_to_delete = []

    for thread in EmailThread.objects.all():
        messages = thread.get_ordered_messages()

        if len(messages) == 1 and isinstance(messages[0], OutgoingEmailMessage):
            # Only one message and it's outgoing
            threads_to_delete.append(thread.id)

    # Bulk delete the filtered threads
    if threads_to_delete:
        deleted_count, _ = EmailThread.objects.filter(id__in=threads_to_delete).delete()
        print(f"🧹 Deleted {deleted_count} threads with 0 or only 1 outgoing message.")
    else:
        print("✅ No empty or single-outgoing-message threads found.")

