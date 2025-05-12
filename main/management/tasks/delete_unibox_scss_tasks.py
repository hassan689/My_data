from django_q.models import Schedule, Success



# Unibox that repeats after every 3 minute to check/fetch fresh emails from the mailboxes
def delete_unibox_scss_tasks(schedule_name):
    try:
        schedule = Schedule.objects.get(name=schedule_name)
        task_name = schedule.func  # This is the function path as string
        deleted_count, _ = Success.objects.filter(func=task_name).delete()
        return f"Deleted {deleted_count} success logs for task: {task_name}"
    except Schedule.DoesNotExist:
        return f"Scheduled task '{schedule_name}' not found"


