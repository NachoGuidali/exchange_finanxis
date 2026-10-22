"""
Compra, venta y swap. Cada función es atómica: saldo, movimientos, mayor, apunte de
renta y boleto se confirman juntos o no se confirma nada (OperacionError → rollback).
"""
from decimal import Decimal

from django.db import transaction

from usuarios.models import CuentaContable, Usuario
from usuarios.services.accounting import (
    registrar_comision_swap,
    registrar_spread_compra,
    registrar_spread_venta,
)
from usuarios.services.boletos import emitir_boleto
from usuarios.services.comun import (
    OperacionError,
    fmt_ccy,
    fmt_money,
    gen_numero_boleto,
    q2,
    snapshot_boleto,
)
from usuarios.services.ledger import (
    CAMPO_SALDO,
    cuenta_sistema,
    cuenta_usuario,
    registrar_asiento,
)
from usuarios.utils import registrar_movimiento

MONEDAS = ('USDT', 'USD')
SWAP_RATE = Decimal('1.00')  # paridad USD/USDT


def _bloquear_usuario(usuario):
    return Usuario.objects.select_for_update().get(pk=usuario.pk)


def _mover(u, moneda, delta):
    """Aplica delta al saldo cacheado y devuelve (antes, despues)."""
    campo = CAMPO_SALDO[moneda]
    antes = getattr(u, campo)
    setattr(u, campo, q2(antes + delta))
    return antes, getattr(u, campo)


@transaction.atomic
def comprar(usuario, moneda, monto_ars, cotizacion):
    """El cliente paga ARS y recibe `moneda` al precio de VENTA publicado."""
    if moneda not in MONEDAS:
        raise OperacionError("Moneda inválida.")
    precio = Decimal(cotizacion.venta)
    precio_ref = Decimal(cotizacion.ref_venta or cotizacion.venta)

    u = _bloquear_usuario(usuario)
    if monto_ars <= 0 or u.saldo_ars < monto_ars:
        raise OperacionError("Saldo ARS insuficiente o monto inválido")

    recibido = q2(Decimal(monto_ars) / precio)
    if recibido <= 0:
        raise OperacionError("El monto es demasiado chico para esta cotización.")

    ars_antes, ars_despues = _mover(u, 'ARS', -monto_ars)
    ccy_antes, ccy_despues = _mover(u, moneda, recibido)
    u.save(update_fields=['saldo_ars', CAMPO_SALDO[moneda]])

    registrar_movimiento(
        usuario=u, tipo='compra', moneda='ARS', monto=-monto_ars,
        descripcion=f'Compra de {moneda} a ${precio}',
        saldo_antes=ars_antes, saldo_despues=ars_despues
    )
    mov_ccy = registrar_movimiento(
        usuario=u, tipo='compra', moneda=moneda, monto=recibido,
        descripcion=f'Compra de {moneda} con ${monto_ars} ARS',
        saldo_antes=ccy_antes, saldo_despues=ccy_despues
    )

    cu, mesa = cuenta_usuario(u), cuenta_sistema(CuentaContable.MESA)
    registrar_asiento('compra', f'Compra de {recibido} {moneda} por ${monto_ars} ARS', [
        (cu, 'ARS', ars_despues - ars_antes), (mesa, 'ARS', ars_antes - ars_despues),
        (cu, moneda, ccy_despues - ccy_antes), (mesa, moneda, ccy_antes - ccy_despues),
    ], referencia=f'Movimiento:{mov_ccy.pk}')

    # renta de la casa por spread (en ARS)
    registrar_spread_compra(
        usuario=u, moneda_ccy=moneda, monto_ars=monto_ars,
        ref_price=precio_ref, applied_price=precio,
        movimiento=mov_ccy, detalle_extra={"cot_id": cotizacion.id}
    )

    snapshot = snapshot_boleto(
        u, titulo=f'Compra de {moneda}',
        debitado=fmt_money(monto_ars), origen=fmt_money(monto_ars),
        tasa=f"{q2(precio)} ARS / {moneda}", destino=fmt_ccy(recibido, moneda),
    )
    tipo_bol = 'compra_ars_usdt' if moneda == 'USDT' else 'compra_ars_usd'
    emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_ccy)
    return mov_ccy


@transaction.atomic
def vender(usuario, moneda, monto_ccy, cotizacion):
    """El cliente entrega `moneda` y recibe ARS al precio de COMPRA publicado."""
    if moneda not in MONEDAS:
        raise OperacionError("Moneda inválida.")
    precio = Decimal(cotizacion.compra)
    precio_ref = Decimal(cotizacion.ref_compra or cotizacion.compra)

    u = _bloquear_usuario(usuario)
    if monto_ccy <= 0:
        raise OperacionError("Monto inválido")
    if getattr(u, CAMPO_SALDO[moneda]) < monto_ccy:
        raise OperacionError(f"Saldo {moneda} insuficiente")

    ars_recibe = q2(Decimal(monto_ccy) * precio)
    ccy_antes, ccy_despues = _mover(u, moneda, -monto_ccy)
    ars_antes, ars_despues = _mover(u, 'ARS', ars_recibe)
    u.save(update_fields=['saldo_ars', CAMPO_SALDO[moneda]])

    registrar_movimiento(
        usuario=u, tipo='venta', moneda=moneda, monto=-monto_ccy,
        descripcion=f'Venta de {moneda} a ${precio}',
        saldo_antes=ccy_antes, saldo_despues=ccy_despues
    )
    mov_ars = registrar_movimiento(
        usuario=u, tipo='venta', moneda='ARS', monto=ars_recibe,
        descripcion=f'Venta de {moneda}. ARS acreditado.',
        saldo_antes=ars_antes, saldo_despues=ars_despues
    )

    cu, mesa = cuenta_usuario(u), cuenta_sistema(CuentaContable.MESA)
    registrar_asiento('venta', f'Venta de {monto_ccy} {moneda} por ${ars_recibe} ARS', [
        (cu, moneda, ccy_despues - ccy_antes), (mesa, moneda, ccy_antes - ccy_despues),
        (cu, 'ARS', ars_despues - ars_antes), (mesa, 'ARS', ars_antes - ars_despues),
    ], referencia=f'Movimiento:{mov_ars.pk}')

    registrar_spread_venta(
        usuario=u, moneda_ccy=moneda, monto_ccy=monto_ccy,
        ref_price=precio_ref, applied_price=precio,
        movimiento=mov_ars, detalle_extra={"cot_id": cotizacion.id}
    )

    snapshot = snapshot_boleto(
        u, titulo=f'Venta de {moneda}',
        debitado=fmt_ccy(monto_ccy, moneda), origen=fmt_ccy(monto_ccy, moneda),
        tasa=f"{q2(precio)} ARS / {moneda}", destino=fmt_money(ars_recibe),
    )
    tipo_bol = 'venta_usdt_ars' if moneda == 'USDT' else 'venta_usd_ars'
    emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_ars)
    return mov_ars


SWAPS = {
    # dirección: (moneda que entrega, moneda que recibe, tipo de boleto)
    'USD_to_USDT': ('USD', 'USDT', 'swap_usd_usdt'),
    'USDT_to_USD': ('USDT', 'USD', 'swap_usdt_usd'),
}


@transaction.atomic
def swap(usuario, direccion, amount, *, fee_bps):
    if direccion not in SWAPS:
        raise OperacionError("Dirección de swap inválida")
    origen, destino, tipo_bol = SWAPS[direccion]
    fee_bps = Decimal(fee_bps)

    u = _bloquear_usuario(usuario)
    if amount <= 0:
        raise OperacionError("El monto debe ser mayor a 0.")
    if getattr(u, CAMPO_SALDO[origen]) < amount:
        raise OperacionError(f"Saldo {origen} insuficiente")

    bruto = amount * SWAP_RATE if origen == 'USD' else amount / SWAP_RATE
    neto = q2(bruto * (Decimal('1') - fee_bps / Decimal('10000')))
    fee = q2(bruto - neto)

    org_antes, org_despues = _mover(u, origen, -amount)
    dst_antes, dst_despues = _mover(u, destino, neto)
    u.save(update_fields=[CAMPO_SALDO[origen], CAMPO_SALDO[destino]])

    desc = f'Swap {origen}→{destino} al rate {SWAP_RATE}, fee {fee_bps} bps'
    registrar_movimiento(
        usuario=u, tipo='venta', moneda=origen, monto=-amount, descripcion=desc,
        saldo_antes=org_antes, saldo_despues=org_despues
    )
    mov_dest = registrar_movimiento(
        usuario=u, tipo='compra', moneda=destino, monto=neto, descripcion=desc,
        saldo_antes=dst_antes, saldo_despues=dst_despues
    )

    cu, mesa = cuenta_usuario(u), cuenta_sistema(CuentaContable.MESA)
    registrar_asiento('swap', desc, [
        (cu, origen, org_despues - org_antes), (mesa, origen, org_antes - org_despues),
        (cu, destino, dst_despues - dst_antes),
        (cuenta_sistema(CuentaContable.COMISIONES), destino, fee),
        (mesa, destino, -(dst_despues - dst_antes) - fee),
    ], referencia=f'Movimiento:{mov_dest.pk}')

    registrar_comision_swap(
        usuario=u, direccion=direccion, fee_amount=fee, fee_currency=destino,
        movimiento=mov_dest, detalle_extra={"swap_fee_bps": int(fee_bps)}
    )

    snapshot = snapshot_boleto(
        u, titulo=f'Swap {origen} → {destino}',
        debitado=fmt_ccy(amount, origen), origen=fmt_ccy(amount, origen),
        tasa=f"{SWAP_RATE} USDT / USD — fee {fee_bps} bps", destino=fmt_ccy(neto, destino),
        comision=f"{fee} {destino}" if fee > 0 else None,
    )
    emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_dest)
    return mov_dest
