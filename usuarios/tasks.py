from celery import shared_task
from django.core.management import call_command


@shared_task
def actualizar_cotizaciones_task():
    call_command('actualizar_cotizacion')


@shared_task
def conciliar_saldos_task():
    """Corre a diario: si un saldo no coincide con el mayor, queda un ERROR en los logs."""
    import logging

    from usuarios.models import Usuario
    from usuarios.services.ledger import diferencias_de_conciliacion

    logger = logging.getLogger(__name__)
    difs = diferencias_de_conciliacion(Usuario.objects.all())
    for u, moneda, cache, libro in difs:
        logger.error("Conciliación: %s (id %s) %s saldo=%s mayor=%s", u.username, u.pk, moneda, cache, libro)
    return len(difs)
