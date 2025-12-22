import json
import requests
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from django.core.management.base import BaseCommand
from django.utils.timezone import now
from usuarios.models import Cotizacion, ExchangeConfig

Q2 = lambda x: Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
Q6 = lambda x: Decimal(x).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

def aplicar_spread(precio_ref: Decimal, bps: int, sentido: str) -> Decimal:
    """
    sentido:
      - 'compra' => precio que paga la casa al cliente (menor que ref)
      - 'venta'  => precio que cobra la casa al cliente (mayor que ref)
    """
    precio_ref = Decimal(precio_ref)
    factor = Decimal(bps) / Decimal(10000)
    if sentido == 'compra':
        return Q2(precio_ref * (Decimal('1') - factor))
    return Q2(precio_ref * (Decimal('1') + factor))

class Command(BaseCommand):
    help = 'Actualiza la cotización de USDT (Binance P2P) y USD (DolarAPI) aplicando spreads de ExchangeConfig'

    def handle(self, *args, **kwargs):
        cfg = ExchangeConfig.current()
        self.actualizar_usdt(cfg.spread_bps_usdt)
        self.actualizar_usd(cfg.spread_bps_usd)

    # ---------- USDT (Binance P2P) ----------
    def actualizar_usdt(self, spread_bps: int):
        url = 'https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search'
        payload = {
            "asset": "USDT",
            "fiat": "ARS",
            "tradeType": "SELL",   # vendedores de USDT → cliente COMPRA USDT
            "page": 1,
            "rows": 10,
            "payTypes": []
        }
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        try:
            r = requests.post(url, data=json.dumps(payload), headers=headers, timeout=12)
            r.raise_for_status()
            data = r.json().get('data', []) or []

            precios = []
            for item in data:
                adv = item.get('adv') or {}
                p = adv.get('price')
                if p:
                    precios.append(Decimal(str(p)))

            if not precios:
                self.stdout.write(self.style.WARNING("No se encontraron precios de USDT en Binance."))
                return

            # referencia: promedio simple de los top N
            ref = Q6(sum(precios) / len(precios))

            compra = aplicar_spread(ref, spread_bps, 'compra')
            venta  = aplicar_spread(ref, spread_bps, 'venta')

            Cotizacion.objects.create(
                moneda='USDT',
                compra=compra,
                venta=venta,
                fecha=now(),
                ref_compra=ref,     # guardamos ref para auditoría/contabilidad
                ref_venta=ref,
                margin_bps=spread_bps
            )

            self.stdout.write(self.style.SUCCESS(
                f"[USDT] ref={ref} • compra={compra} • venta={venta} • bps={spread_bps}"
            ))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[ERROR USDT Binance] {e}"))

    # ---------- USD (DolarAPI) ----------
    def actualizar_usd(self, spread_bps: int):
        url = "https://dolarapi.com/v1/dolares"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()

            oficial = next((d for d in data if d.get('casa') == 'oficial'), None)
            if not oficial:
                self.stdout.write(self.style.WARNING("No se encontró la cotización 'oficial' en dolarapi.com"))
                return

            compra_raw = Decimal(str(oficial['compra']))
            venta_raw  = Decimal(str(oficial['venta']))

            # referencia: mid-price entre compra y venta oficiales
            ref = Q6((compra_raw + venta_raw) / Decimal('2'))

            compra = aplicar_spread(ref, spread_bps, 'compra')
            venta  = aplicar_spread(ref, spread_bps, 'venta')

            Cotizacion.objects.create(
                moneda='USD',
                compra=compra,
                venta=venta,
                fecha=now(),
                ref_compra=ref,
                ref_venta=ref,
                margin_bps=spread_bps
            )

            self.stdout.write(self.style.SUCCESS(
                f"[USD] ref={ref} • compra={compra} • venta={venta} • bps={spread_bps}"
            ))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[ERROR USD DolarAPI] {e}"))
