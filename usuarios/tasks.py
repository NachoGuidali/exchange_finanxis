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


@shared_task(bind=True, max_retries=30, default_retry_delay=60)
def verificar_deposito_usdt_task(self, deposito_id):
    """Verifica on-chain; si faltan confirmaciones o la API falló, reintenta cada minuto (30 veces)."""
    from usuarios.models import DepositoUSDT
    from usuarios.services.onchain import verificar_deposito

    dep = DepositoUSDT.objects.filter(pk=deposito_id, estado='pendiente').first()
    if dep is None:
        return 'ya procesado'
    res = verificar_deposito(dep)
    if res.estado in ('pendiente', 'error'):
        raise self.retry()
    return res.estado
