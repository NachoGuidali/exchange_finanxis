import uuid
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.utils.timezone import localtime

from usuarios.utils import cliente_ctx


class OperacionError(Exception):
    """Validación de negocio fallida. Al lanzarse dentro de una transacción, hace rollback."""


def q2(x):
    return Decimal(x).quantize(Decimal('0.01'), rounding=ROUND_DOWN)


def fmt_money(x, symbol='$'):
    return f"{symbol}{q2(Decimal(x)):,.2f}"


def fmt_ccy(x, ccy):
    return f"{q2(Decimal(x))} {ccy}"


def gen_numero_boleto():
    return f"BOL-{localtime().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"


def psav_ctx():
    return {
        'nombre': settings.EMPRESA_NOMBRE,
        'cuit': settings.EMPRESA_CUIT,
        'domicilio': settings.EMPRESA_DOMICILIO,
        'contacto': settings.SUPPORT_CONTACTO,
        'leyenda_psav': settings.PSAV_LEYENDA or '',
    }


def snapshot_boleto(usuario, *, titulo, debitado, origen, tasa, destino, comision=None):
    """Datos de negocio que se imprimen en el boleto (todos ya formateados)."""
    return {
        'titulo': titulo,
        'estado': 'Completo',
        'monto_debitado_fmt': debitado,
        'comision_total_fmt': comision,
        'monto_origen_fmt': origen,
        'tasa_fmt': tasa,
        'monto_destino_fmt': destino,
        'cliente': cliente_ctx(usuario),
        'psav': psav_ctx(),
    }
