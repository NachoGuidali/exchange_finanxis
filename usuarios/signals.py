from decimal import Decimal
from django.db.models.signals import post_save
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.dispatch import receiver
from .models import ApunteExchange, CuentaExchange, DepositoARS, DepositoUSDT, RetiroARS, RetiroCrypto, Cotizacion

@receiver(post_save, sender=ApunteExchange)
def acumular_en_cuenta(sender, instance, created, **kwargs):
    if not created:
        return
    # Solo categorías que representan INGRESO/EGRESO DEL EXCHANGE:
    # - spread_compra / spread_venta → ARS
    # - fee_swap → en la moneda del fee (USD o USDT)
    # - ajuste → puede ser +/-, respetamos la moneda
    if instance.categoria not in {'spread_compra', 'spread_venta', 'fee_swap', 'ajuste'}:
        # entrada/salida (depósitos / retiros) NO tocan la caja del exchange
        return

    cta, _ = CuentaExchange.objects.get_or_create(nombre="Exchange")

    # sumamos según moneda del apunte (puede ser negativo en ajuste)
    if instance.moneda == "ARS":
        cta.saldo_ars = (cta.saldo_ars or 0) + instance.monto_moneda
    elif instance.moneda == "USDT":
        cta.saldo_usdt = (cta.saldo_usdt or 0) + instance.monto_moneda
    else:
        cta.saldo_usd = (cta.saldo_usd or 0) + instance.monto_moneda

    cta.save()


def _ref_ars_ccy(moneda: str) -> Decimal:
    """ARS por 1 unidad de la moneda informada, usando la última cotización disponible."""
    if moneda == 'ARS':
        return Decimal('1')
    c = Cotizacion.objects.filter(moneda=moneda).order_by('-fecha').first()
    if not c:
        return Decimal('0')
    # criterio de referencia (podés ajustar): prefiero ref_venta / ref_compra si existen
    ref = c.ref_venta or c.ref_compra or c.venta or c.compra
    return Decimal(ref)

def _ya_posteado(tipo: str, model_name: str, obj_id: int) -> bool:
    """
    Chequea si ya registramos un apunte para ese documento de negocio.
    Guardamos en extra {doc, model, id} para idempotencia.
    """
    return ApunteExchange.objects.filter(
        extra__doc=tipo, extra__model=model_name, extra__id=obj_id
    ).exists()

def _marcar_entrada(*, importe: Decimal, moneda: str, instance, detalle: str):
    """Entrada global (depósitos acreditados). No afecta caja Exchange."""
    ref = _ref_ars_ccy(moneda)
    importe_ars = (importe * ref) if moneda != 'ARS' else importe

    def _create():
        ApunteExchange.objects.create(
            categoria='entrada',
            importe=importe,             # alias de monto_moneda (property)
            moneda=moneda,
            importe_ars=importe_ars,     # alias de monto_ars (property)
            detalle=detalle,
            usuario=getattr(instance, 'usuario', None),
            movimiento=None,
            ref_price=(ref if moneda != 'ARS' else None),
            applied_price=None,
            extra={'doc': 'deposito', 'model': instance.__class__.__name__, 'id': instance.id},
            fecha=timezone.now(),
        )

    # Evitar duplicados y crear tras commit de DB:
    if not _ya_posteado('deposito', instance.__class__.__name__, instance.id):
        transaction.on_commit(_create)

def _marcar_salida(*, importe: Decimal, moneda: str, instance, detalle: str):
    """Salida global (retiros enviados). No afecta caja Exchange."""
    ref = _ref_ars_ccy(moneda)
    importe_ars = (importe * ref) if moneda != 'ARS' else importe

    def _create():
        ApunteExchange.objects.create(
            categoria='salida',
            importe=-importe,            # egreso → negativo (alias de monto_moneda)
            moneda=moneda,
            importe_ars=-(importe_ars),  # egreso → negativo (alias de monto_ars)
            detalle=detalle,
            usuario=getattr(instance, 'usuario', None),
            movimiento=None,
            ref_price=(ref if moneda != 'ARS' else None),
            applied_price=None,
            extra={'doc': 'retiro', 'model': instance.__class__.__name__, 'id': instance.id},
            fecha=timezone.now(),
        )

    if not _ya_posteado('retiro', instance.__class__.__name__, instance.id):
        transaction.on_commit(_create)

# -------- Signals --------
# REGLA: depósitos cuentan cuando el estado está en **aprobado** (o si nacen aprobados)

@receiver(post_save, sender=DepositoARS)
def deposito_ars_creado_o_aprobado(sender, instance: DepositoARS, created, **kwargs):
    # si el modelo de depósito nace en "aprobado" o lo cambian luego
    if (created and getattr(instance, 'estado', None) == 'aprobado') \
       or (getattr(instance, 'estado', None) == 'aprobado'):
        _marcar_entrada(
            importe=instance.monto, moneda='ARS', instance=instance,
            detalle=f"Depósito ARS aprobado #{instance.id}"
        )

@receiver(post_save, sender=DepositoUSDT)
def deposito_usdt_creado_o_aprobado(sender, instance: DepositoUSDT, created, **kwargs):
    if (created and getattr(instance, 'estado', None) == 'aprobado') \
       or (getattr(instance, 'estado', None) == 'aprobado'):
        _marcar_entrada(
            importe=instance.monto, moneda='USDT', instance=instance,
            detalle=f"Depósito USDT aprobado #{instance.id}"
        )

# Retiros: cuentan cuando pasan a "enviado" o "completado"

@receiver(post_save, sender=RetiroARS)
def retiro_ars_enviado_o_completado(sender, instance: RetiroARS, created, **kwargs):
    if getattr(instance, 'estado', None) in ('enviado', 'completado'):
        _marcar_salida(
            importe=instance.monto, moneda='ARS', instance=instance,
            detalle=f"Retiro ARS {instance.estado} #{instance.id}"
        )

@receiver(post_save, sender=RetiroCrypto)
def retiro_crypto_enviado_o_completado(sender, instance: RetiroCrypto, created, **kwargs):
    if getattr(instance, 'estado', None) in ('enviado', 'completado'):
        moneda = getattr(instance, 'moneda', 'USDT')  # ej. 'USDT' o 'USD'
        _marcar_salida(
            importe=instance.monto, moneda=moneda, instance=instance,
            detalle=f"Retiro {moneda} {instance.estado} #{instance.id}"
        )

# -------- (opcional) Acumulador de caja exchange --------
# OJO: Caja Exchange SOLO se alimenta de spread/fee/ajuste (no de entradas/salidas globales).
# Si tenías un acumulador automático, APLÍCALE FILTRO por categoría.
@receiver(post_save, sender=ApunteExchange)
def acumular_en_cuenta_solo_rentas(sender, instance: ApunteExchange, created, **kwargs):
    if not created:
        return
    if instance.categoria not in ('spread_compra', 'spread_venta', 'fee_swap', 'ajuste'):
        return  # ignorar entradas/salidas globales

    cta, _ = CuentaExchange.objects.get_or_create(nombre="Exchange")
    if instance.moneda == "ARS":
        cta.saldo_ars = (cta.saldo_ars or Decimal('0')) + instance.monto_moneda
    elif instance.moneda == "USDT":
        cta.saldo_usdt = (cta.saldo_usdt or Decimal('0')) + instance.monto_moneda
    else:
        cta.saldo_usd = (cta.saldo_usd or Decimal('0')) + instance.monto_moneda
    cta.save()