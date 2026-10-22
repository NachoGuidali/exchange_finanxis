"""
Libro mayor de doble entrada. Ver la explicación de la convención de signos en models.py.

Toda operación que cambia un saldo de usuario debe llamar a `registrar_asiento` dentro
de la misma transacción en la que actualiza Usuario.saldo_*.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from usuarios.models import Asiento, CuentaContable, LineaAsiento

CAMPO_SALDO = {'ARS': 'saldo_ars', 'USDT': 'saldo_usdt', 'USD': 'saldo_usd'}


class AsientoDesbalanceado(ValueError):
    pass


def cuenta_usuario(usuario):
    cuenta, _ = CuentaContable.objects.get_or_create(
        usuario=usuario, defaults={'codigo': f'usuario:{usuario.pk}'}
    )
    return cuenta


def cuenta_sistema(codigo):
    cuenta, _ = CuentaContable.objects.get_or_create(codigo=codigo, usuario=None)
    return cuenta


@transaction.atomic
def registrar_asiento(tipo, descripcion, lineas, *, referencia='', creado_por=None):
    """
    lineas: iterable de (cuenta, moneda, monto). Montos en 0 se omiten.
    Lanza AsientoDesbalanceado si alguna moneda no suma 0.
    """
    lineas = [(c, m, Decimal(monto)) for c, m, monto in lineas if Decimal(monto) != 0]
    if not lineas:
        raise AsientoDesbalanceado("Un asiento necesita al menos una línea con monto.")

    sumas = defaultdict(Decimal)
    for _, moneda, monto in lineas:
        if moneda not in CAMPO_SALDO:
            raise AsientoDesbalanceado(f"Moneda inválida: {moneda}")
        sumas[moneda] += monto
    desbalance = {m: s for m, s in sumas.items() if s != 0}
    if desbalance:
        raise AsientoDesbalanceado(f"Asiento '{descripcion}' desbalanceado: {desbalance}")

    asiento = Asiento.objects.create(
        tipo=tipo, descripcion=descripcion[:255], referencia=referencia, creado_por=creado_por
    )
    LineaAsiento.objects.bulk_create(
        LineaAsiento(asiento=asiento, cuenta=c, moneda=m, monto=monto) for c, m, monto in lineas
    )
    return asiento


def saldo(cuenta, moneda):
    total = LineaAsiento.objects.filter(cuenta=cuenta, moneda=moneda).aggregate(s=Sum('monto'))['s']
    return total or Decimal('0')


def saldos_por_usuario():
    """{(usuario_id, moneda): saldo} según el mayor."""
    filas = (LineaAsiento.objects.filter(cuenta__usuario__isnull=False)
             .values('cuenta__usuario_id', 'moneda').annotate(s=Sum('monto')))
    return {(f['cuenta__usuario_id'], f['moneda']): f['s'] for f in filas}


def diferencias_de_conciliacion(usuarios):
    """Lista de (usuario, moneda, saldo_cacheado, saldo_mayor) que no coinciden."""
    mayor = saldos_por_usuario()
    difs = []
    for u in usuarios:
        for moneda, campo in CAMPO_SALDO.items():
            cache = getattr(u, campo) or Decimal('0')
            libro = mayor.get((u.pk, moneda), Decimal('0'))
            if cache != libro:
                difs.append((u, moneda, cache, libro))
    return difs
