from celery import shared_task
import subprocess

@shared_task
def actualizar_cotizaciones_task():
    subprocess.call(['python', 'manage.py', 'actualizar_cotizacion'])
