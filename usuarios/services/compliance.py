"""
Límites operativos y alertas de prevención de lavado (UIF / PSAV).

Los umbrales se configuran en ExchangeConfig (panel admin) y, el de volumen mensual,
también por usuario. Las alertas NO bloquean la operación: quedan para que el Oficial
de Cumplimiento las revise y, si corresponde, reporte. Los límites sí bloquean.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Abs
from django.utils import timezone

from usuarios.models import AlertaCompliance, Cotizacion, ExchangeConfig, Movimiento, RetiroARS, RetiroCrypto
from usuarios.services.comun import OperacionError

VENTANA_FRACCIONAMIENTO = timedelta(hours=24)
MIN_OPERACIONES_FRACCIONAMIENTO = 3


class LimiteExcedido(OperacionError):
    """La operación supera un límite. La vista registra la alerta fuera de la transacción."""

    def __init__(self, mensaje, *, monto_ars):
        super().__init__(mensaje)
        self.monto_ars = monto_ars


def valor_ars(moneda, monto):
    """Valuación en ARS con la última cotización de referencia."""
    if moneda == 'ARS':
        return Decimal(monto)
    c = Cotizacion.objects.filter(moneda=moneda).order_by('-fecha').first()
    if not c:
        return Decimal('0')
    return Decimal(monto) * Decimal(c.ref_compra or c.compra)


# ---------------- Límites (bloquean) ----------------

def volumen_operado_30d(usuario):
    desde = timezone.now() - timedelta(days=30)
    total = (Movimiento.objects
             .filter(usuario=usuario, moneda='ARS', tipo__in=['compra', 'venta'], fecha__gte=desde)
             .aggregate(s=Sum(Abs('monto')))['s'])
    return total or Decimal('0')


def retirado_24h_ars(usuario):
    desde = timezone.now() - timedelta(hours=24)
    total = (RetiroARS.objects.filter(usuario=usuario, fecha_solicitud__gte=desde)
             .exclude(estado='rechazado').aggregate(s=Sum('monto'))['s'] or Decimal('0'))
    for r in (RetiroCrypto.objects.filter(usuario=usuario, fecha_solicitud__gte=desde)
              .exclude(estado='rechazado').values('moneda').annotate(s=Sum('monto'))):
        total += valor_ars(r['moneda'], r['s'])
    return total


def verificar_limite_operacion(usuario, monto_ars):
    cfg = ExchangeConfig.current()
    limite = usuario.limite_mensual_ars if usuario.limite_mensual_ars is not None else cfg.limite_mensual_operado_ars
    if volumen_operado_30d(usuario) + monto_ars > limite:
        raise LimiteExcedido(
            "La operación supera tu límite de volumen mensual. Contactá a soporte para ampliarlo.",
            monto_ars=monto_ars,
        )


def verificar_limite_retiro(usuario, monto_ars):
    cfg = ExchangeConfig.current()
    if retirado_24h_ars(usuario) + monto_ars > cfg.limite_diario_retiro_ars:
        raise LimiteExcedido(
            "El retiro supera el límite diario. Probá con un monto menor o contactá a soporte.",
            monto_ars=monto_ars,
        )


def alerta_limite(usuario, error: LimiteExcedido, referencia=''):
    return AlertaCompliance.objects.create(
        usuario=usuario, tipo='limite_excedido', detalle=str(error)[:255],
        monto_ars=error.monto_ars, referencia=referencia,
    )


# ---------------- Alertas (no bloquean) ----------------

def _ya_alertado(usuario, tipo, desde):
    return AlertaCompliance.objects.filter(usuario=usuario, tipo=tipo, fecha__gte=desde).exists()


def evaluar(usuario, *, descripcion, monto_ars, referencia=''):
    """Llamar después de cada operación, depósito acreditado o retiro solicitado."""
    cfg = ExchangeConfig.current()
    umbral = cfg.umbral_alerta_ars
    alertas = []

    if monto_ars >= umbral:
        alertas.append(AlertaCompliance.objects.create(
            usuario=usuario, tipo='umbral', monto_ars=monto_ars, referencia=referencia,
            detalle=f"{descripcion}: ${monto_ars:,.2f} (umbral ${umbral:,.2f})"[:255],
        ))
        return alertas

    # Fraccionamiento: varias operaciones chicas en 24 h que juntas llegan al umbral
    desde = timezone.now() - VENTANA_FRACCIONAMIENTO
    movs = (Movimiento.objects.filter(usuario=usuario, fecha__gte=desde)
            .exclude(monto=0).exclude(tipo='ajuste').values('tipo', 'moneda', 'monto'))
    # una operación de compra/venta genera dos movimientos: contamos solo la pata en ARS
    relevantes = [m for m in movs if m['tipo'] in ('deposito', 'retiro') or m['moneda'] == 'ARS']
    total = sum((abs(valor_ars(m['moneda'], m['monto'])) for m in relevantes), Decimal('0'))
    if (len(relevantes) >= MIN_OPERACIONES_FRACCIONAMIENTO and total >= umbral
            and not _ya_alertado(usuario, 'fraccionamiento', desde)):
        alertas.append(AlertaCompliance.objects.create(
            usuario=usuario, tipo='fraccionamiento', monto_ars=total, referencia=referencia,
            detalle=f"{len(relevantes)} movimientos en 24 h suman ${total:,.2f} (umbral ${umbral:,.2f})"[:255],
        ))
    return alertas


def evaluar_retiro(usuario, *, descripcion, monto_ars, referencia=''):
    alertas = evaluar(usuario, descripcion=descripcion, monto_ars=monto_ars, referencia=referencia)
    desde = timezone.now() - timedelta(hours=24)
    deposito_reciente = Movimiento.objects.filter(
        usuario=usuario, tipo='deposito', monto__gt=0, fecha__gte=desde
    ).exists()
    if deposito_reciente and not _ya_alertado(usuario, 'retiro_rapido', desde):
        alertas.append(AlertaCompliance.objects.create(
            usuario=usuario, tipo='retiro_rapido', monto_ars=monto_ars, referencia=referencia,
            detalle=f"{descripcion} dentro de las 24 h de un depósito acreditado"[:255],
        ))
    return alertas
