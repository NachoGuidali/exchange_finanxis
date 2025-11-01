from decimal import Decimal, ROUND_HALF_UP
from django.utils.timezone import now
from usuarios.models import ApunteExchange, Cotizacion

Q2 = lambda x: Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
Q6 = lambda x: Decimal(x).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

def _ref_precio(moneda: str) -> Decimal:
    """
    ARS por 1 CCY (ref actual). Toma la última cotización registrada.
    Para normalizar importes en ARS cuando la ganancia es en USD/USDT.
    """
    if moneda == 'ARS':
        return Decimal('1')
    c = Cotizacion.objects.filter(moneda=moneda).order_by('-fecha').first()
    if not c:
        return Decimal('0')
    # usamos ref_venta como referencia de valuación (podés ajustar criterio)
    ref = c.ref_venta or c.ref_compra or c.venta or c.compra
    return Decimal(ref)

def registrar_spread_compra(*, usuario, moneda_ccy: str, monto_ars: Decimal,
                            ref_price: Decimal, applied_price: Decimal,
                            movimiento=None, detalle_extra: dict|None=None) -> ApunteExchange:
    """
    Cliente COMPRA CCY pagando ARS.
    Ingreso Exchange (ARS) = ARS cobrado - (CCY entregado * precio_ref).
    """
    recibido_ccy = Decimal(monto_ars) / Decimal(applied_price) if applied_price else Decimal('0')
    costo_ars = Q6(recibido_ccy * Decimal(ref_price))
    ingreso_ars = Q6(Decimal(monto_ars) - costo_ars)

    return ApunteExchange.objects.create(
        categoria='spread_compra',
        importe=ingreso_ars,                # positivo si hay margen a favor
        moneda='ARS',
        importe_ars=ingreso_ars,           # ya está en ARS
        detalle=f"Spread compra {moneda_ccy}. ARS={monto_ars} @applied {applied_price} vs ref {ref_price}",
        usuario=usuario,
        movimiento=movimiento,
        ref_price=ref_price,
        applied_price=applied_price,
        extra=detalle_extra or {}
    )

def registrar_spread_venta(*, usuario, moneda_ccy: str, monto_ccy: Decimal,
                           ref_price: Decimal, applied_price: Decimal,
                           movimiento=None, detalle_extra: dict|None=None) -> ApunteExchange:
    """
    Cliente VENDE CCY y recibe ARS.
    Ingreso Exchange (ARS) = (CCY * ref_price) - (CCY * applied_price).
    """
    ingreso_ars = Q6(Decimal(monto_ccy) * (Decimal(ref_price) - Decimal(applied_price)))

    return ApunteExchange.objects.create(
        categoria='spread_venta',
        importe=ingreso_ars,
        moneda='ARS',
        importe_ars=ingreso_ars,
        detalle=f"Spread venta {moneda_ccy}. CCY={monto_ccy} @applied {applied_price} vs ref {ref_price}",
        usuario=usuario,
        movimiento=movimiento,
        ref_price=ref_price,
        applied_price=applied_price,
        extra=detalle_extra or {}
    )

def registrar_comision_swap(*, usuario, direccion: str, fee_amount: Decimal,
                            fee_currency: str, movimiento=None, detalle_extra: dict|None=None) -> ApunteExchange:
    """
    Fee cobrado en un swap. Se normaliza a ARS con ref del momento.
    """
    ref = _ref_precio(fee_currency)  # ARS por 1 fee_currency
    ingreso_ars = Q6(Decimal(fee_amount) * Decimal(ref))

    return ApunteExchange.objects.create(
        categoria='fee_swap',
        importe=Q6(fee_amount),
        moneda=fee_currency,
        importe_ars=ingreso_ars,
        detalle=f"Fee swap {direccion}: {fee_amount} {fee_currency} (ref {ref} ARS/{fee_currency})",
        usuario=usuario,
        movimiento=movimiento,
        ref_price=ref,
        applied_price=None,
        extra=detalle_extra or {}
    )