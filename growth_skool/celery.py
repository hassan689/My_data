from __future__ import absolute_import, unicode_literals
import os
from celery import Celery
from django.db import connections
from django.db.utils import OperationalError
from celery import signals

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'growth_skool.settings')

# Create a Celery app instance
app = Celery('growth_skool')

# Load task modules from all registered Django app configs.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps (looks for tasks.py files)
app.autodiscover_tasks()

app.conf.timezone = 'Asia/Karachi'


@signals.worker_process_init.connect
def close_db_connections(**kwargs):
    for conn in connections.all():
        try:
            conn.close()
        except OperationalError:
            pass

