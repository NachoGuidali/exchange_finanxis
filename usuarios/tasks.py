from celery import shared_task
from django.core.management import call_command


@shared_task
def actualizar_cotizaciones_task():
    call_command('actualizar_cotizacion')
