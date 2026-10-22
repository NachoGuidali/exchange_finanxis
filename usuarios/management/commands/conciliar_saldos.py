import sys

from django.core.management.base import BaseCommand

from usuarios.models import Usuario
from usuarios.services.ledger import diferencias_de_conciliacion


class Command(BaseCommand):
    help = "Compara Usuario.saldo_* contra el libro mayor. Sale con código 1 si hay diferencias."

    def handle(self, *args, **opts):
        difs = diferencias_de_conciliacion(Usuario.objects.all())
        for u, moneda, cache, libro in difs:
            self.stdout.write(self.style.ERROR(
                f"{u.username} (id {u.pk}) {moneda}: saldo={cache} mayor={libro} dif={cache - libro}"
            ))
        if difs:
            self.stdout.write(self.style.ERROR(f"{len(difs)} diferencia(s) de conciliación."))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("Saldos conciliados con el mayor."))
