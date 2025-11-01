import json, math, time
import requests
from django.core.management.base import BaseCommand

BASE = "https://apis.datos.gob.ar/georef/api"

def get_json(path, params=None, timeout=20):
    url = f"{BASE}{path}"
    r = requests.get(url, params=params or {}, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.HTTPError:
        raise SystemExit(f"[HTTP {r.status_code}] {url}\nparams={params}\nbody={r.text[:400]}...")
    return r.json()

class Command(BaseCommand):
    help = "Descarga provincias y localidades de Georef-AR y las serializa a geo_ar.json"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="geo_ar.json")
        parser.add_argument("--sleep", type=float, default=0.25)

    def handle(self, *args, **opts):
        out = opts["out"]
        delay = float(opts["sleep"])

        self.stdout.write("Descargando provincias...")
        provs = get_json("/provincias", {"campos":"id,nombre","orden":"nombre","max":100})["provincias"]

        result = {"paises":[{"iso2":"AR","nombre":"Argentina","provincias":[]}]}

        for p in provs:
            prov_id = p["id"]
            prov_nombre = p["nombre"]
            self.stdout.write(f" · {prov_nombre}: calculando total localidades…")

            meta = get_json("/localidades", {"provincia":prov_id,"max":1})
            total = meta.get("cantidad", 0)

            localidades = []
            if total > 0:
                max_page = 500
                pages = int(math.ceil(total / max_page))
                for i in range(pages):
                    inicio = i * max_page
                    data = get_json("/localidades", {
                        "provincia": prov_id, "campos":"nombre", "orden":"nombre",
                        "max": max_page, "inicio": inicio
                    })
                    localidades.extend([l["nombre"] for l in data.get("localidades", [])])
                    time.sleep(delay)

            result["paises"][0]["provincias"].append({
                "nombre": prov_nombre,
                "georef_id": prov_id,
                "localidades": localidades
            })

        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        self.stdout.write(self.style.SUCCESS(f"OK. Guardado {out}"))