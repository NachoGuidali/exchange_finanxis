from django.core.management.base import BaseCommand

from usuarios.services.cotizaciones import FUENTES, FuenteNoDisponible, actualizar


class Command(BaseCommand):
    help = 'Actualiza la cotización de USDT (Binance) y USD (DolarAPI) aplicando spreads de ExchangeConfig'

    def handle(self, *args, **kwargs):
        for moneda in FUENTES:
            try:
                cot, fuente = actualizar(moneda)
            except FuenteNoDisponible as e:
                self.stderr.write(self.style.ERROR(f"[{moneda}] sin cotización: {e}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"[{moneda}] {fuente}: ref={cot.ref_venta} • compra={cot.compra} • "
                f"venta={cot.venta} • bps={cot.margin_bps}"
            ))
