from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.shortcuts import redirect, render
from django.utils import timezone

from usuarios.models import Notificacion


MONEDAS_CRYPTO = ('USDT', 'USD')
MONTO_MAXIMO = Decimal('9999999999.99')  # entra en todos los DecimalField de montos


def decimales_de(moneda):
    """USDT se maneja con 6 decimales (como en la blockchain); pesos y dólares, con 2."""
    return 6 if moneda == 'USDT' else 2


def monto_csv(valor, moneda):
    """Monto para CSV con la precisión de la moneda (punto decimal, sin separador de miles)."""
    if valor is None:
        return ''
    return f"{Decimal(valor).quantize(Decimal(1).scaleb(-decimales_de(moneda))):f}"


def parse_monto(raw, decimales=2):
    """
    Convierte el monto que manda el cliente a Decimal.
    Devuelve None si no es un número finito, positivo, con `decimales` decimales como máximo
    y no mayor a MONTO_MAXIMO.
    """
    try:
        monto = Decimal(str(raw).strip().replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not monto.is_finite() or monto <= 0 or monto > MONTO_MAXIMO or monto.as_tuple().exponent < -decimales:
        return None
    return monto


def kyc_required(view):
    """Exige email confirmado, declaración jurada UIF y KYC aprobado para operar con saldo."""
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.email_confirmed:
            return redirect('verify_email_notice')
        if request.user.declaracion_vigente is None:
            return redirect('declaracion_jurada')
        if request.user.estado_verificacion != 'aprobado':
            return render(request, 'usuarios/no_verificado.html')
        return view(request, *args, **kwargs)
    return _wrapped


def cotizacion_vigente(cot):
    """True si la cotización existe y no superó COTIZACION_MAX_EDAD_MIN (un precio manual activo no vence)."""
    if cot is None:
        return False
    if cot.manual_activa:
        return True
    max_edad = timedelta(minutes=getattr(settings, 'COTIZACION_MAX_EDAD_MIN', 15))
    return timezone.now() - cot.fecha <= max_edad



def registrar_movimiento(usuario, tipo, moneda, monto, descripcion="", admin=None, saldo_antes=None, saldo_despues=None,
                         operacion=None):
    from .models import Movimiento

    return Movimiento.objects.create(
        operacion=operacion,
        usuario=usuario,
        tipo=tipo,
        moneda=moneda,
        monto=monto,
        descripcion=descripcion,
        admin_responsable=admin,
        saldo_antes=saldo_antes,
        saldo_despues=saldo_despues
    )


def crear_notificacion(usuario, mensaje):
    Notificacion.objects.create(usuario=usuario, mensaje=mensaje)


def cliente_ctx(u):
    nombre = (f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}").strip() or u.username
    return {
        "nombre": nombre,
        "doc_tipo": getattr(u, "doc_tipo", "DNI"),
        "doc_nro":  getattr(u, "doc_nro", "—"),
        "domicilio": getattr(u, "domicilio", "—"),
        "email": getattr(u, "email", "") or "—",
        "telefono": getattr(u, "telefono", "—"),
        "estado_verificacion": getattr(u, "estado_verificacion", "pendiente"),
    }    


