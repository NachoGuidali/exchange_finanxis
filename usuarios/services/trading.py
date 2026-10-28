"""
Compra, venta y swap. Cada función es atómica: saldo, movimientos, mayor, apunte de
renta y boleto se confirman juntos o no se confirma nada (OperacionError → rollback).
"""
from datetime import timedelta
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from usuarios.models import CotizacionReservada, CuentaContable, Usuario
from usuarios.services.accounting import (
    registrar_comision_swap,
    registrar_spread_compra,
    registrar_spread_venta,
)
from usuarios.services import avisos, compliance
from usuarios.services.boletos import emitir_boleto
from usuarios.services.comun import (
    OperacionError,
    fmt_ccy,
    fmt_money,
    gen_numero_boleto,
    q2,
    q_moneda,
    snapshot_boleto,
)
from usuarios.services.ledger import (
    CAMPO_SALDO,
    cuenta_sistema,
    cuenta_usuario,
    registrar_asiento,
)
from usuarios.templatetags.finanxis import money
from usuarios.utils import registrar_movimiento

MONEDAS = ('USDT', 'USD')
SWAP_RATE = Decimal('1.00')  # paridad USD/USDT


def _avisar_operacion(u, mensaje, detalle, boleto):
    url = avisos.url_absoluta('ver_comprobante', boleto.numero) if boleto else None
    avisos.cliente(u, mensaje, asunto="Operación realizada", detalle=detalle, url=url, boton="Ver comprobante")


def _bloquear_usuario(usuario):
    return Usuario.objects.select_for_update().get(pk=usuario.pk)


def _mover(u, moneda, delta):
    """Aplica delta al saldo cacheado y devuelve (antes, despues)."""
    campo = CAMPO_SALDO[moneda]
    antes = getattr(u, campo)
    setattr(u, campo, q_moneda(antes + delta, moneda))
    return antes, getattr(u, campo)


def _precios(operacion, cotizacion):
    """(precio aplicado, precio de referencia): el cliente compra a VENTA y vende a COMPRA."""
    if operacion == 'compra':
        return Decimal(cotizacion.venta), Decimal(cotizacion.ref_venta or cotizacion.venta)
    return Decimal(cotizacion.compra), Decimal(cotizacion.ref_compra or cotizacion.compra)


def _resultado(operacion, monto, precio, moneda):
    """Lo que recibe el cliente: la moneda al comprar, pesos al vender."""
    if operacion == 'compra':
        return q_moneda(Decimal(monto) / precio, moneda)
    return q2(Decimal(monto) * precio)


def cotizar(usuario, operacion, moneda, monto, cotizacion):
    """
    Bloquea el precio por COTIZACION_RESERVA_SEG segundos. Valida saldo y límites de entrada
    para que el cliente no confirme algo que va a fallar (se vuelven a validar al ejecutar).
    """
    if operacion not in ('compra', 'venta'):
        raise OperacionError("Operación inválida.")
    if moneda not in MONEDAS:
        raise OperacionError("Moneda inválida.")
    precio, precio_ref = _precios(operacion, cotizacion)
    resultado = _resultado(operacion, monto, precio, moneda)
    if resultado <= 0:
        raise OperacionError("El monto es demasiado chico para esta cotización.")

    u = Usuario.objects.get(pk=usuario.pk)  # saldo actual, no el del objeto recibido
    moneda_entrega = 'ARS' if operacion == 'compra' else moneda
    if getattr(u, CAMPO_SALDO[moneda_entrega]) < monto:
        raise OperacionError(f"Saldo {moneda_entrega} insuficiente")
    compliance.verificar_limite_operacion(u, monto if operacion == 'compra' else resultado)

    return CotizacionReservada.objects.create(
        usuario=u, operacion=operacion, moneda=moneda, monto=monto,
        precio=precio, precio_ref=precio_ref, resultado=resultado, cotizacion=cotizacion,
        expira=timezone.now() + timedelta(seconds=getattr(settings, 'COTIZACION_RESERVA_SEG', 30)),
    )


@transaction.atomic
def ejecutar_cotizacion(usuario, cotizacion_id):
    """Ejecuta la operación al precio reservado, si sigue vigente. Una reserva se usa una sola vez."""
    reserva = CotizacionReservada.objects.select_for_update().filter(pk=cotizacion_id, usuario=usuario).first()
    if reserva is None:
        raise OperacionError("Cotización inexistente.")
    if reserva.usada_en is not None:
        raise OperacionError("Esta cotización ya fue utilizada.")
    if timezone.now() >= reserva.expira:
        raise OperacionError("La cotización expiró. Pedí una nueva.")

    reserva.usada_en = timezone.now()
    reserva.save(update_fields=['usada_en'])
    fn = comprar if reserva.operacion == 'compra' else vender
    return fn(usuario, reserva.moneda, reserva.monto, precio=reserva.precio,
              precio_ref=reserva.precio_ref, cotizacion_id=reserva.cotizacion_id)


@transaction.atomic
def comprar(usuario, moneda, monto_ars, *, precio, precio_ref, cotizacion_id):
    """El cliente paga ARS y recibe `moneda` al precio indicado (el de VENTA publicado)."""
    if moneda not in MONEDAS:
        raise OperacionError("Moneda inválida.")

    u = _bloquear_usuario(usuario)
    if monto_ars <= 0 or u.saldo_ars < monto_ars:
        raise OperacionError("Saldo ARS insuficiente o monto inválido")
    compliance.verificar_limite_operacion(u, monto_ars)

    recibido = _resultado('compra', monto_ars, precio, moneda)
    if recibido <= 0:
        raise OperacionError("El monto es demasiado chico para esta cotización.")

    ars_antes, ars_despues = _mover(u, 'ARS', -monto_ars)
    ccy_antes, ccy_despues = _mover(u, moneda, recibido)
    u.save(update_fields=['saldo_ars', CAMPO_SALDO[moneda]])

    op = uuid.uuid4()
    registrar_movimiento(
        usuario=u, tipo='compra', moneda='ARS', monto=-monto_ars, operacion=op,
        descripcion=f'Compra de {moneda} a ${precio}',
        saldo_antes=ars_antes, saldo_despues=ars_despues
    )
    mov_ccy = registrar_movimiento(
        usuario=u, tipo='compra', moneda=moneda, monto=recibido, operacion=op,
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
        movimiento=mov_ccy, detalle_extra={"cot_id": cotizacion_id}
    )
    compliance.evaluar(u, descripcion=f'Compra de {moneda}', monto_ars=monto_ars,
                       referencia=f'Movimiento:{mov_ccy.pk}')

    snapshot = snapshot_boleto(
        u, titulo=f'Compra de {moneda}',
        debitado=fmt_money(monto_ars), origen=fmt_money(monto_ars),
        tasa=f"{q2(precio)} ARS / {moneda}", destino=fmt_ccy(recibido, moneda),
    )
    tipo_bol = 'compra_ars_usdt' if moneda == 'USDT' else 'compra_ars_usd'
    boleto = emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_ccy)
    _avisar_operacion(u, f"Compraste {money(recibido)} {moneda} a $ {money(precio)}.",
                      f"Pagaste $ {money(monto_ars)}.", boleto)
    return mov_ccy


@transaction.atomic
def vender(usuario, moneda, monto_ccy, *, precio, precio_ref, cotizacion_id):
    """El cliente entrega `moneda` y recibe ARS al precio indicado (el de COMPRA publicado)."""
    if moneda not in MONEDAS:
        raise OperacionError("Moneda inválida.")

    u = _bloquear_usuario(usuario)
    if monto_ccy <= 0:
        raise OperacionError("Monto inválido")
    if getattr(u, CAMPO_SALDO[moneda]) < monto_ccy:
        raise OperacionError(f"Saldo {moneda} insuficiente")

    ars_recibe = _resultado('venta', monto_ccy, precio, moneda)
    compliance.verificar_limite_operacion(u, ars_recibe)
    ccy_antes, ccy_despues = _mover(u, moneda, -monto_ccy)
    ars_antes, ars_despues = _mover(u, 'ARS', ars_recibe)
    u.save(update_fields=['saldo_ars', CAMPO_SALDO[moneda]])

    op = uuid.uuid4()
    registrar_movimiento(
        usuario=u, tipo='venta', moneda=moneda, monto=-monto_ccy, operacion=op,
        descripcion=f'Venta de {moneda} a ${precio}',
        saldo_antes=ccy_antes, saldo_despues=ccy_despues
    )
    mov_ars = registrar_movimiento(
        usuario=u, tipo='venta', moneda='ARS', monto=ars_recibe, operacion=op,
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
        movimiento=mov_ars, detalle_extra={"cot_id": cotizacion_id}
    )
    compliance.evaluar(u, descripcion=f'Venta de {moneda}', monto_ars=ars_recibe,
                       referencia=f'Movimiento:{mov_ars.pk}')

    snapshot = snapshot_boleto(
        u, titulo=f'Venta de {moneda}',
        debitado=fmt_ccy(monto_ccy, moneda), origen=fmt_ccy(monto_ccy, moneda),
        tasa=f"{q2(precio)} ARS / {moneda}", destino=fmt_money(ars_recibe),
    )
    tipo_bol = 'venta_usdt_ars' if moneda == 'USDT' else 'venta_usd_ars'
    boleto = emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_ars)
    _avisar_operacion(u, f"Vendiste {money(monto_ccy)} {moneda} a $ {money(precio)}.",
                      f"Recibiste $ {money(ars_recibe)} en tu saldo.", boleto)
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
    neto = q_moneda(bruto * (Decimal('1') - fee_bps / Decimal('10000')), destino)
    fee = q_moneda(bruto - neto, destino)

    org_antes, org_despues = _mover(u, origen, -amount)
    dst_antes, dst_despues = _mover(u, destino, neto)
    u.save(update_fields=[CAMPO_SALDO[origen], CAMPO_SALDO[destino]])

    desc = f'Swap {origen}→{destino} al rate {SWAP_RATE}, fee {fee_bps} bps'
    op = uuid.uuid4()
    registrar_movimiento(
        usuario=u, tipo='venta', moneda=origen, monto=-amount, descripcion=desc, operacion=op,
        saldo_antes=org_antes, saldo_despues=org_despues
    )
    mov_dest = registrar_movimiento(
        usuario=u, tipo='compra', moneda=destino, monto=neto, descripcion=desc, operacion=op,
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
    boleto = emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov_dest)
    _avisar_operacion(u, f"Convertiste {money(amount)} {origen} a {money(neto)} {destino}.",
                      f"Comisión: {money(fee)} {destino}.", boleto)
    return mov_dest
