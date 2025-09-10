import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_exchange.settings')

app = Celery('core_exchange')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
