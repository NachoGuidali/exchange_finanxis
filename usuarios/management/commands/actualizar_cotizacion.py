import requests
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils.timezone import now
from usuarios.models import Cotizacion

class Command(BaseCommand):
    help = 'Actualiza la cotización de USDT (Binance) y USD (DolarAPI)'

    def handle(self, *args, **kwargs):
        self.actualizar_usdt()
        self.actualizar_usd()

    def actualizar_usdt(self):
        url = 'https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search'
        data = {
            "asset": "USDT",
            "fiat": "ARS",
            "tradeType": "SELL",
            "payTypes": [],
            "page": 1,
            "rows": 5
        }

        headers = {
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)
            response.raise_for_status()
            results = response.json().get('data', [])

            precios = [Decimal(ad['adv']['price']) for ad in results if 'adv' in ad and 'price' in ad['adv']]

            if not precios:
                self.stdout.write(self.style.WARNING("No se encontraron precios de USDT en Binance."))
                return

            promedio = sum(precios) / len(precios)
            comision = Decimal('0.02')
            compra = round(promedio * (1 - comision), 2)
            venta = round(promedio * (1 + comision), 2)

            Cotizacion.objects.create(
                moneda='USDT',
                compra=compra,
                venta=venta,
                fecha=now()
            )

            self.stdout.write(self.style.SUCCESS(f"[USDT] Compra: {compra} - Venta: {venta}"))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[ERROR USDT Binance] {e}"))

    def actualizar_usd(self):
        try:
            url = "https://dolarapi.com/v1/dolares"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            for item in data:
                if item.get('casa') == 'oficial':
                    compra_raw = Decimal(item['compra'])
                    venta_raw = Decimal(item['venta'])

                    comision = Decimal('0.02')
                    compra = round(compra_raw * (1 - comision), 2)
                    venta = round(venta_raw * (1 + comision), 2)

                    Cotizacion.objects.create(
                        moneda='USD',
                        compra=compra,
                        venta=venta,
                        fecha=now()
                    )

                    self.stdout.write(self.style.SUCCESS(
                        f"[USD] Oficial - Compra: {compra} / Venta: {venta}"
                    ))
                    return

            self.stdout.write(self.style.WARNING("No se encontró la cotización oficial en dolarapi.com"))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[ERROR USD DolarAPI] {e}"))
