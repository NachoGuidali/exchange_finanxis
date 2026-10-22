from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from usuarios.models import Asiento, CuentaContable, LineaAsiento, RetiroARS, RetiroCrypto, Usuario
from usuarios.services.ledger import CAMPO_SALDO, cuenta_sistema, cuenta_usuario, registrar_asiento

REF_RETIROS = 'apertura:retiros_pendientes'


class Command(BaseCommand):
    help = ("Registra en el mayor los saldos existentes antes de que existiera el libro "
            "(una sola vez por usuario; es seguro correrlo varias veces).")

    @transaction.atomic
    def handle(self, *args, **opts):
        ajustes = cuenta_sistema(CuentaContable.AJUSTES)
        con_libro = set(LineaAsiento.objects.filter(cuenta__usuario__isnull=False)
                        .values_list('cuenta__usuario_id', flat=True))
        n = 0
        for u in Usuario.objects.exclude(pk__in=con_libro).select_for_update():
            lineas = []
            for moneda, campo in CAMPO_SALDO.items():
                s = getattr(u, campo) or Decimal('0')
                lineas += [(cuenta_usuario(u), moneda, s), (ajustes, moneda, -s)]
            if any(monto for _, _, monto in lineas):
                registrar_asiento('apertura', f'Saldo de apertura de {u.username}', lineas,
                                  referencia=f'apertura:usuario:{u.pk}')
                n += 1

        # Retiros que ya habían descontado saldo y siguen abiertos
        if not Asiento.objects.filter(referencia=REF_RETIROS).exists():
            pendientes = cuenta_sistema(CuentaContable.RETIROS_PENDIENTES)
            totales = {'ARS': RetiroARS.objects.filter(estado__in=['pendiente', 'aprobado'])
                                            .aggregate(s=Sum('monto'))['s'] or Decimal('0')}
            for f in (RetiroCrypto.objects.filter(estado='pendiente')
                      .values('moneda').annotate(s=Sum('monto'))):
                totales[f['moneda']] = f['s']
            lineas = [x for m, s in totales.items() for x in ((pendientes, m, s), (ajustes, m, -s))]
            if any(monto for _, _, monto in lineas):
                registrar_asiento('apertura', 'Retiros abiertos al crear el mayor', lineas,
                                  referencia=REF_RETIROS)

        self.stdout.write(self.style.SUCCESS(f"Apertura registrada para {n} usuario(s)."))
