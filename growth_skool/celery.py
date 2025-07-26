from __future__ import absolute_import, unicode_literals
import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'growth_skool.settings')

# Create a Celery app instance
app = Celery('growth_skool')

# Load task modules from all registered Django app configs.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps (looks for tasks.py files)
app.autodiscover_tasks()

# --- NEW: Celery Beat Settings ---
app.conf.beat_schedule = {
    'launch-scheduled-campaigns-every-minute': {
        'task': 'dashboard.tasks.launch_scheduled_campaign_checker',
        'schedule': 60.0,  # Run every 60 seconds (1 minute)
        # 'args': (some_arg,) # If your checker task needed arguments
        'options': {'queue': 'celery'}, # Optionally specify a queue for beat tasks
    },
}
app.conf.timezone = 'Asia/Karachi'
