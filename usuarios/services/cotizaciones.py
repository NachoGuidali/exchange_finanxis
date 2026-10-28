"""
Actualización de cotizaciones: USDT (Binance P2P, con Binance spot y el dólar cripto de DolarAPI de respaldo) y USD oficial
(DolarAPI), con el spread de ExchangeConfig aplicado. Lo usan el comando
`actualizar_cotizacion`, la task de Celery y, en modo demo, la vista de operar.
"""
import json
import logging
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.core.cache import cache
from django.utils import timezone

from usuarios.models import Cotizacion, ExchangeConfig

logger = logging.getLogger(__name__)

Q2 = lambda x: Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # noqa: E731
Q6 = lambda x: Decimal(x).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)  # noqa: E731
HEADERS = {'User-Agent': 'finanxis-cotizador/1.0', 'Accept': 'application/json'}


class FuenteNoDisponible(Exception):
    pass


def aplicar_spread(precio_ref: Decimal, bps: int, sentido: str) -> Decimal:
    """
    sentido:
      - 'compra' => precio que paga la casa al cliente (menor que ref)
      - 'venta'  => precio que cobra la casa al cliente (mayor que ref)
    """
    factor = Decimal(bps) / Decimal(10000)
    if sentido == 'compra':
        return Q2(Decimal(precio_ref) * (Decimal('1') - factor))
    return Q2(Decimal(precio_ref) * (Decimal('1') + factor))


def _ref_usdt_p2p():
    payload = {"asset": "USDT", "fiat": "ARS", "tradeType": "SELL", "page": 1, "rows": 10, "payTypes": []}
    r = requests.post('https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search',
                      data=json.dumps(payload), headers={**HEADERS, 'Content-Type': 'application/json'},
                      timeout=12)
    r.raise_for_status()
    precios = [Decimal(str(i['adv']['price'])) for i in (r.json().get('data') or [])
               if (i.get('adv') or {}).get('price')]
    if not precios:
        raise FuenteNoDisponible("Binance P2P no devolvió precios")
    # referencia: promedio simple de los top N
    return Q6(sum(precios) / len(precios)), 'Binance P2P'


def _ref_usdt_spot():
    r = requests.get('https://api.binance.com/api/v3/ticker/bookTicker', params={'symbol': 'USDTARS'},
                     headers=HEADERS, timeout=12)
    r.raise_for_status()
    d = r.json()
    return Q6((Decimal(d['bidPrice']) + Decimal(d['askPrice'])) / 2), 'Binance spot'


def _ref_usd_oficial():
    r = requests.get("https://dolarapi.com/v1/dolares", headers=HEADERS, timeout=10)
    r.raise_for_status()
    oficial = next((d for d in r.json() if d.get('casa') == 'oficial'), None)
    if not oficial:
        raise FuenteNoDisponible("DolarAPI no devolvió la cotización oficial")
    # referencia: mid-price entre compra y venta oficiales
    return Q6((Decimal(str(oficial['compra'])) + Decimal(str(oficial['venta']))) / 2), 'DolarAPI oficial'


def _ref_usdt_dolar_cripto():
    # último respaldo: el "dólar cripto" de DolarAPI sigue al USDT/ARS de los exchanges locales.
    # Útil donde Binance no responde (p. ej. servidores en EE.UU. o detrás de un proxy).
    r = requests.get("https://dolarapi.com/v1/dolares/cripto", headers=HEADERS, timeout=10)
    r.raise_for_status()
    d = r.json()
    if not d.get('compra') or not d.get('venta'):
        raise FuenteNoDisponible("DolarAPI no devolvió el dólar cripto")
    return Q6((Decimal(str(d['compra'])) + Decimal(str(d['venta']))) / 2), 'DolarAPI cripto'


FUENTES = {
    'USDT': (_ref_usdt_p2p, _ref_usdt_spot, _ref_usdt_dolar_cripto),
    'USD': (_ref_usd_oficial,),
}


class PrecioManualActivo(Exception):
    pass


def ultima(moneda):
    return Cotizacion.objects.filter(moneda=moneda).order_by('-fecha', '-id').first()


def actualizar(moneda):
    """
    Crea una Cotizacion nueva desde la primera fuente que responda. Devuelve (cotizacion, fuente).
    Si un admin fijó un precio manual que sigue activo, no lo pisa (PrecioManualActivo).
    """
    actual = ultima(moneda)
    if actual and actual.manual_activa:
        raise PrecioManualActivo(f"{moneda} tiene un precio manual activo")
    cfg = ExchangeConfig.current()
    bps = cfg.spread_bps_usdt if moneda == 'USDT' else cfg.spread_bps_usd
    errores = []
    for fuente in FUENTES[moneda]:
        try:
            ref, nombre = fuente()
            break
        except (requests.RequestException, FuenteNoDisponible, KeyError, ValueError, ArithmeticError) as e:
            errores.append(f"{fuente.__name__}: {e}")
    else:
        raise FuenteNoDisponible("; ".join(errores))

    cot = Cotizacion.objects.create(
        moneda=moneda,
        compra=aplicar_spread(ref, bps, 'compra'),
        venta=aplicar_spread(ref, bps, 'venta'),
        ref_compra=ref,  # guardamos ref para auditoría/contabilidad
        ref_venta=ref,
        margin_bps=bps,
        fuente=nombre,
    )
    return cot, nombre


def fijar_manual(moneda, compra, venta, hasta, admin):
    """Precio cargado por un admin. La referencia contable es el punto medio entre compra y venta."""
    if moneda not in FUENTES:
        raise ValueError("Moneda inválida.")
    if not (0 < compra < venta):
        raise ValueError("La compra tiene que ser menor que la venta.")
    ref = Q6((compra + venta) / 2)
    return Cotizacion.objects.create(
        moneda=moneda, compra=Q2(compra), venta=Q2(venta), ref_compra=ref, ref_venta=ref,
        margin_bps=int(((venta - compra) / 2 / ref * 10000).to_integral_value(ROUND_HALF_UP)),
        fuente='Manual', manual=True, manual_hasta=hasta, cargada_por=admin,
    )


def quitar_manual(moneda):
    """Termina el precio manual activo. Devuelve la cotización que lo tenía, o None."""
    actual = ultima(moneda)
    if not (actual and actual.manual_activa):
        return None
    actual.manual_hasta = timezone.now()
    actual.save(update_fields=['manual_hasta'])
    return actual


def actualizar_si_vencida(cotizaciones_vigentes):
    """
    Modo demo / sin Celery: si la cotización venció, la actualiza en el momento.
    Un lock en cache evita que varias requests simultáneas golpeen las APIs a la vez.
    """
    if cotizaciones_vigentes or not cache.add('cotizacion_actualizando', 1, timeout=60):
        return False
    try:
        for moneda in FUENTES:
            try:
                actualizar(moneda)
            except PrecioManualActivo:
                pass
            except FuenteNoDisponible as e:
                logger.warning("No se pudo actualizar la cotización %s: %s", moneda, e)
        return True
    finally:
        cache.delete('cotizacion_actualizando')
