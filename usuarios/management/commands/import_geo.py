import json
from django.core.management.base import BaseCommand
from usuarios.models import Pais, Provincia, Localidad
from django.db import transaction

class Command(BaseCommand):
    help = "Importa geo_ar.json a las tablas Pais/Provincia/Localidad"

    def add_arguments(self, parser):
        parser.add_argument("--json", required=True, help="Archivo generado por fetch_geo_ar")

    @transaction.atomic
    def handle(self, *args, **opts):
        path = opts["json"]
        self.stdout.write(f"Abriendo {path}…")
        data = json.load(open(path, "r", encoding="utf-8"))

        count_p = count_pr = count_l = 0

        for p in data.get("paises", []):
            pais, _ = Pais.objects.get_or_create(iso2=p["iso2"], defaults={"nombre": p["nombre"]})
            if _:
                count_p += 1

            for prov in p.get("provincias", []):
                provincia, created_prov = Provincia.objects.get_or_create(
                    pais=pais, nombre=prov["nombre"],
                    defaults={"georef_id": prov.get("georef_id","")}
                )
                if created_prov:
                    count_pr += 1

                # Localidades
                for loc in prov.get("localidades", []):
                    _, created_loc = Localidad.objects.get_or_create(
                        provincia=provincia, nombre=loc
                    )
                    if created_loc:
                        count_l += 1

        self.stdout.write(self.style.SUCCESS(
            f"Listo. Nuevos: paises={count_p}, provincias={count_pr}, localidades={count_l}"
        ))