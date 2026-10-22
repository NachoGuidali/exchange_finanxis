"""
Carga datos FICTICIOS para mostrar la app (nunca usar con datos reales).
Las operaciones pasan por los mismos servicios que en producción, así el mayor,
los boletos y los paneles quedan consistentes.
"""
import io
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from PIL import Image

from usuarios.models import Cotizacion, CuentaContable, DepositoARS, Localidad, Usuario
from usuarios.services import fondos, ledger, trading
from usuarios.services.cotizaciones import FuenteNoDisponible, actualizar

USUARIOS = [
    # username, nombre, apellido, staff, kyc
    ("demo_admin", "Ana", "Administradora", True, "aprobado"),
    ("demo_cliente", "Carlos", "Cliente", False, "aprobado"),
    ("demo_nuevo", "Nadia", "Nueva", False, "pendiente"),
]


def _imagen_demo(texto):
    """PNG chiquito para comprobantes ficticios."""
    buf = io.BytesIO()
    Image.new("RGB", (320, 120), (240, 240, 240)).save(buf, format="PNG")
    return ContentFile(buf.getvalue(), name=f"{texto}.png")


class Command(BaseCommand):
    help = "Crea usuarios y operaciones de demostración (solo con DEMO_MODE=True)."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Contraseña para los usuarios demo (si no, se genera una).")

    def handle(self, *args, **opts):
        if not getattr(settings, "DEMO_MODE", False):
            raise CommandError("Este comando solo corre con DEMO_MODE=True: crea usuarios con datos ficticios.")
        if Usuario.objects.filter(username="demo_cliente").exists():
            self.stdout.write(self.style.WARNING("Los datos demo ya estaban cargados."))
            return

        password = opts.get("password") or secrets.token_urlsafe(9)
        self._geo()
        self._cotizaciones()
        with transaction.atomic():
            usuarios = self._usuarios(password)
            self._historial(usuarios)
        call_command("conciliar_saldos")

        self.stdout.write(self.style.SUCCESS("\nDatos demo cargados. Usuarios (misma contraseña):"))
        for username, *_ in USUARIOS:
            self.stdout.write(f"  {username:<14} {password}")

    def _geo(self):
        if not Localidad.objects.exists():
            call_command("import_geo", json=str(settings.BASE_DIR / "geo_ar.json"), verbosity=0)

    def _cotizaciones(self):
        for moneda, ref in (("USDT", Decimal("1600")), ("USD", Decimal("1500"))):
            try:
                actualizar(moneda)
            except FuenteNoDisponible:
                # sin internet: valores fijos para que la demo funcione igual
                Cotizacion.objects.create(moneda=moneda, compra=ref * Decimal("0.98"), venta=ref * Decimal("1.02"),
                                          ref_compra=ref, ref_venta=ref, margin_bps=200)

    def _usuarios(self, password):
        loc = Localidad.objects.select_related("provincia__pais").filter(nombre__iexact="La Plata").first() \
            or Localidad.objects.select_related("provincia__pais").first()
        creados = {}
        for i, (username, nombre, apellido, staff, kyc) in enumerate(USUARIOS, start=1):
            u = Usuario.objects.create_user(
                username, f"{username}@demo.finanxis.local", password,
                first_name=nombre, last_name=apellido, is_staff=staff, is_superuser=staff,
                estado_verificacion=kyc, email_confirmed=True,
                doc_tipo="DNI", doc_nro=f"9999000{i}", telefono="+5491100000000",
                calle="Calle Falsa", numero_calle=str(100 + i), codigo_postal="1900",
                pais=loc.provincia.pais if loc else None, provincia=loc.provincia if loc else None, localidad=loc,
                tyc_aceptado=True, tyc_version=settings.TYC_VERSION,
            )
            creados[username] = u
        return creados

    def _historial(self, u):
        admin, cliente = u["demo_admin"], u["demo_cliente"]
        ledger.cuenta_sistema(CuentaContable.TESORERIA)

        # depósito aprobado y uno pendiente para que el admin tenga algo que revisar
        dep = DepositoARS.objects.create(usuario=cliente, monto=Decimal("500000"), comprobante=_imagen_demo("dep1"))
        fondos.aprobar_deposito_ars(dep.pk, admin)
        DepositoARS.objects.create(usuario=cliente, monto=Decimal("150000"), comprobante=_imagen_demo("dep2"))

        cot_usdt = Cotizacion.objects.filter(moneda="USDT").latest("fecha")
        cot_usd = Cotizacion.objects.filter(moneda="USD").latest("fecha")
        r = trading.cotizar(cliente, "compra", "USDT", Decimal("200000"), cot_usdt)
        trading.ejecutar_cotizacion(cliente, r.id)
        r = trading.cotizar(cliente, "compra", "USD", Decimal("100000"), cot_usd)
        trading.ejecutar_cotizacion(cliente, r.id)
        cliente.refresh_from_db()
        r = trading.cotizar(cliente, "venta", "USDT", Decimal("20"), cot_usdt)
        trading.ejecutar_cotizacion(cliente, r.id)
        trading.swap(cliente, "USD_to_USDT", Decimal("10"), fee_bps=100)

        # retiro pendiente para el panel admin
        fondos.solicitar_retiro_ars(cliente, Decimal("30000"), alias="carlos.demo.mp", banco="Banco Demo")
