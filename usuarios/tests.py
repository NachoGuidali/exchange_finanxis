import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import DepositoARSForm
from .models import (
    ApunteExchange, BoletoOperacion, Cotizacion, CuentaExchange, DepositoARS, ExchangeConfig,
    AlertaCompliance, Movimiento, Notificacion, RegistroAuditoria, RetiroARS, RetiroCrypto, SupportTicket, Usuario,
)
from .utils import parse_monto

MEDIA_TMP = tempfile.mkdtemp(prefix="finanxis-tests-")


def crear_usuario(username="cliente", aprobado=True, **saldos):
    u = Usuario.objects.create_user(
        username, f"{username}@test.com", "Clave-Segura-123",
        email_confirmed=aprobado,
        estado_verificacion="aprobado" if aprobado else "pendiente",
    )
    for campo, valor in saldos.items():
        setattr(u, campo, Decimal(valor))
    u.save()
    if aprobado:
        crear_declaracion(u)
    return u


def crear_declaracion(u, **extra):
    from .models import DeclaracionJurada
    datos = dict(version="test", es_pep=False, es_sujeto_obligado=False, actividad="Empleado",
                 origen_fondos="sueldo", ingresos_mensuales="hasta_1m", volumen_mensual_previsto="hasta_1m",
                 residencia_fiscal_exterior=False, declara_veracidad=True)
    datos.update(extra)
    return DeclaracionJurada.objects.create(usuario=u, **datos)


def crear_cotizaciones(edad=timedelta(0)):
    for moneda, ref in (("USDT", "1000"), ("USD", "900")):
        c = Cotizacion.objects.create(
            moneda=moneda, compra=Decimal(ref) * Decimal("0.98"), venta=Decimal(ref) * Decimal("1.02"),
            ref_compra=Decimal(ref), ref_venta=Decimal(ref), margin_bps=200,
        )
        # fecha es auto_now_add: se ajusta después para simular antigüedad
        Cotizacion.objects.filter(pk=c.pk).update(fecha=timezone.now() - edad)


# Los tests no dependen del .env: sin redirección HTTPS (en CI corre con DEBUG=False)
def operar(client, operacion, moneda, monto):
    """Cotiza y confirma (flujo real en dos pasos). Devuelve la respuesta de la confirmación."""
    resp = client.post(reverse("operar"), {"operacion": operacion, "moneda": moneda, "monto": monto})
    if resp.status_code != 200 or "reserva" not in (resp.context or {}):
        return resp
    return client.post(reverse("confirmar_operacion"), {"reserva": resp.context["reserva"].id})


@override_settings(MEDIA_ROOT=MEDIA_TMP, SECURE_SSL_REDIRECT=False)
class BaseTest(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        cache.clear()


class ParseMontoTests(TestCase):
    def test_valores_validos(self):
        self.assertEqual(parse_monto("10"), Decimal("10"))
        self.assertEqual(parse_monto("10,50"), Decimal("10.50"))

    def test_valores_invalidos(self):
        for raw in (None, "", "abc", "0", "-5", "NaN", "Infinity", "1.001", "1e400"):
            self.assertIsNone(parse_monto(raw), raw)


class RetiroCryptoTests(BaseTest):
    url = reverse("solicitar_retiro_cripto")

    def test_monto_negativo_no_acredita_saldo(self):
        u = crear_usuario(saldo_usdt="10")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "-1000000", "direccion_wallet": "T1", "red": "TRC20"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_usdt, Decimal("10"))
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_moneda_ars_rechazada(self):
        u = crear_usuario(saldo_ars="500000")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "ARS", "monto": "500000", "direccion_wallet": "T1", "red": "TRC20"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("500000"))
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_saldo_insuficiente(self):
        u = crear_usuario(saldo_usdt="5")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "6", "direccion_wallet": "T1", "red": "TRC20"})
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_retiro_ok_descuenta_y_registra_negativo(self):
        u = crear_usuario(saldo_usdt="100")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "40", "direccion_wallet": "T1", "red": "TRC20"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_usdt, Decimal("60"))
        mov = Movimiento.objects.get(usuario=u, tipo="retiro")
        self.assertEqual(mov.monto, Decimal("-40"))

    def test_requiere_kyc(self):
        u = crear_usuario(aprobado=False, saldo_usdt="100")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "40", "direccion_wallet": "T1", "red": "TRC20"})
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_get_no_rompe(self):
        self.client.force_login(crear_usuario())
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_rechazo_recredita_en_la_misma_moneda(self):
        u = crear_usuario(saldo_usdt="100")
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "30", "direccion_wallet": "T1", "red": "TRC20"})
        r = RetiroCrypto.objects.get()
        self.assertEqual(r.red, "TRC20")
        self.client.force_login(admin)
        self.client.post(reverse("rechazar_retiro_cripto", args=[r.id]), {"motivo": "Dirección inválida"})
        u.refresh_from_db()
        self.assertEqual((u.saldo_usdt, u.saldo_usd), (Decimal("100"), Decimal("0")))

    def test_dolares_a_billetera_rechazado_y_red_obligatoria(self):
        u = crear_usuario(saldo_usd="100", saldo_usdt="100")
        self.client.force_login(u)
        # el formulario cripto siempre retira USDT: aunque manden USD, no toca dólares
        self.client.post(self.url, {"moneda": "USD", "monto": "30", "direccion_wallet": "T1", "red": "TRC20"})
        self.client.post(self.url, {"monto": "30", "direccion_wallet": "T1"})  # sin red
        u.refresh_from_db()
        self.assertEqual(u.saldo_usd, Decimal("100"))
        self.assertEqual(RetiroCrypto.objects.filter(moneda="USD").count(), 0)
        self.assertEqual(RetiroCrypto.objects.count(), 1)


class RetiroARSTests(BaseTest):
    url = reverse("solicitar_retiro")

    def test_retiro_en_dolares_a_cuenta_bancaria(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_usd="500", saldo_ars="1000")
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USD", "monto": "200", "cbu": "0" * 22, "banco": "B"})
        u.refresh_from_db()
        self.assertEqual((u.saldo_usd, u.saldo_ars), (Decimal("300"), Decimal("1000")))
        r = RetiroARS.objects.get()
        self.assertEqual((r.moneda, r.cbu), ("USD", "0" * 22))

        self.client.force_login(admin)
        self.client.post(reverse("aprobar_retiro", args=[r.id]))
        with self.settings(BOLETOS_TOLERAR_ERROR_PDF=True), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("enviar_retiro", args=[r.id]), {"titular": "1"})
        r.refresh_from_db()
        self.assertEqual(r.estado, "enviado")
        self.assertEqual(BoletoOperacion.objects.get(usuario=u).tipo, "retiro_usd")
        self.assertTrue(ApunteExchange.objects.filter(categoria="salida", moneda="USD").exists())

    def test_destino_valido(self):
        u = crear_usuario(saldo_ars="1000")
        self.client.force_login(u)
        for datos in ({}, {"alias": "a"}, {"cbu": "123"}, {"alias": "con espacios no"}):
            self.client.post(self.url, {"monto": "10", **datos})
        self.assertFalse(RetiroARS.objects.exists())
        self.client.post(self.url, {"monto": "10", "cbu": "1" * 22})
        self.assertEqual(RetiroARS.objects.get().moneda, "ARS")

    def test_montos_invalidos(self):
        u = crear_usuario(saldo_ars="1000")
        self.client.force_login(u)
        for monto in ("-10", "abc", "", "0"):
            self.client.post(self.url, {"monto": monto, "alias": "mi.alias"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("1000"))
        self.assertFalse(RetiroARS.objects.exists())

    def test_retiro_ok(self):
        u = crear_usuario(saldo_ars="1000")
        self.client.force_login(u)
        self.client.post(self.url, {"monto": "250", "alias": "mi.alias"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("750"))
        self.assertEqual(RetiroARS.objects.get().monto, Decimal("250"))


class OperarTests(BaseTest):
    url = reverse("operar")

    def test_compra_actualiza_saldos_boleto_y_caja(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_ars="10200")
        self.client.force_login(u)
        resp = operar(self.client, "compra", "USDT", "10200")
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("0"))
        self.assertEqual(u.saldo_usdt, Decimal("10"))  # 10200 / 1020

        boleto = BoletoOperacion.objects.get(usuario=u)
        self.assertIsNotNone(boleto.movimiento)
        apunte = ApunteExchange.objects.get(categoria="spread_compra")
        self.assertIsNotNone(apunte.movimiento)
        # 10200 ARS cobrados - 10 USDT * 1000 ref = 200 de spread, acumulado una sola vez
        self.assertEqual(CuentaExchange.objects.get().saldo_ars, Decimal("200"))

    def test_cotizacion_vencida_bloquea(self):
        crear_cotizaciones(edad=timedelta(hours=2))
        u = crear_usuario(saldo_ars="10000")
        self.client.force_login(u)
        self.client.post(self.url, {"operacion": "compra", "moneda": "USDT", "monto": "1000"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("10000"))

    def test_moneda_invalida(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_ars="10000")
        self.client.force_login(u)
        resp = self.client.post(self.url, {"operacion": "compra", "moneda": "ARS", "monto": "1000"})
        self.assertEqual(resp.status_code, 400)

    def test_saldo_insuficiente_no_deja_apunte(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_ars="10")
        self.client.force_login(u)
        operar(self.client, "compra", "USDT", "1000")
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("10"))
        self.assertFalse(ApunteExchange.objects.exists())

    def test_swap_registra_salida_negativa_y_fee_una_vez(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_usd="100")
        self.client.force_login(u)
        self.client.post(self.url, {"operacion": "swap", "swap_direccion": "USD_to_USDT", "monto": "100"})
        u.refresh_from_db()
        self.assertEqual((u.saldo_usd, u.saldo_usdt), (Decimal("0"), Decimal("99")))
        self.assertEqual(Movimiento.objects.get(moneda="USD").monto, Decimal("-100"))
        self.assertEqual(CuentaExchange.objects.get().saldo_usdt, Decimal("1"))


class DepositoTests(BaseTest):
    def test_form_rechaza_monto_negativo(self):
        img = SimpleUploadedFile("c.png", b"x", content_type="image/png")
        form = DepositoARSForm(data={"monto": "-100"}, files={"comprobante": img})
        self.assertIn("monto", form.errors)

    def test_rechazar_deposito_solo_post(self):
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        dep = DepositoARS.objects.create(usuario=admin, monto=10, comprobante="comprobantes/x.png")
        self.client.force_login(admin)
        resp = self.client.get(reverse("rechazar_deposito", args=[dep.id]))
        self.assertEqual(resp.status_code, 405)

    def test_kpis_cuentan_solo_lo_aprobado_y_enviado(self):
        crear_cotizaciones()
        u = crear_usuario(saldo_ars="0")
        DepositoARS.objects.create(usuario=u, monto=500, comprobante="c.png", estado="pendiente")
        DepositoARS.objects.create(usuario=u, monto=1000, comprobante="c.png", estado="aprobado")
        RetiroARS.objects.create(usuario=u, alias="a", monto=300, estado="enviado")
        RetiroARS.objects.create(usuario=u, alias="a", monto=50, estado="rechazado")
        self.client.force_login(u)
        ctx = self.client.get(reverse("dashboard")).context
        self.assertEqual(ctx["kpi_total_ingresado"], Decimal("1000"))
        self.assertEqual(ctx["kpi_total_retirado"], Decimal("300"))


class CuentaTests(BaseTest):
    def test_cambiar_email_exige_reverificar(self):
        u = crear_usuario()
        self.client.force_login(u)
        self.client.post(reverse("cambiar_email"), {"email": "nuevo@test.com", "password": "Clave-Segura-123"})
        u.refresh_from_db()
        self.assertEqual(u.email, "nuevo@test.com")
        self.assertFalse(u.email_confirmed)

    def test_tyc_no_redirige_afuera(self):
        self.client.force_login(crear_usuario())
        resp = self.client.post(reverse("tyc"), {"next": "https://sitio-malicioso.com"})
        self.assertEqual(resp.url, reverse("dashboard"))

    def test_tyc_anonimo_no_rompe(self):
        resp = self.client.post(reverse("tyc"))
        self.assertEqual(resp.status_code, 302)


class MediaPrivadaTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.duenio = crear_usuario("duenio")
        self.duenio.dni_frente = SimpleUploadedFile("dni.png", b"\x89PNG fake", content_type="image/png")
        self.duenio.save()
        self.url = self.duenio.dni_frente.url

    def test_nombre_no_adivinable(self):
        self.assertNotIn("dni.png", self.url)

    def test_anonimo_no_accede(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)  # al login

    def test_otro_usuario_no_accede(self):
        self.client.force_login(crear_usuario("otro"))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_duenio_y_staff_acceden(self):
        self.client.force_login(self.duenio)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Security-Policy"], "sandbox")
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        self.client.force_login(admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_archivo_sin_registro_no_se_sirve(self):
        self.client.force_login(self.duenio)
        self.assertEqual(self.client.get("/media/../core_exchange/settings.py").status_code, 404)


class LoginSeguridadTests(BaseTest):
    def _login(self, password="Clave-Segura-123", username="cliente"):
        return self.client.post(reverse("login"), {"username": username, "password": password})

    def test_bloqueo_tras_intentos_fallidos(self):
        crear_usuario()
        for _ in range(5):
            self._login(password="mala")
        resp = self._login()  # la correcta también queda bloqueada
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_con_2fa(self):
        import pyotp
        u = crear_usuario()
        u.totp_secret = pyotp.random_base32()
        u.totp_activo = True
        u.save()

        resp = self._login()
        self.assertRedirects(resp, reverse("login_2fa"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)  # todavía no entró

        self.client.post(reverse("login_2fa"), {"codigo": "000000"})
        self.assertNotIn("_auth_user_id", self.client.session)

        self.client.post(reverse("login_2fa"), {"codigo": pyotp.TOTP(u.totp_secret).now()})
        self.assertEqual(self.client.session["_auth_user_id"], str(u.pk))

    def test_activar_2fa_requiere_codigo_valido(self):
        import pyotp
        u = crear_usuario()
        self.client.force_login(u)
        self.client.get(reverse("activar_2fa"))
        secret = self.client.session["2fa_secret_nuevo"]
        self.client.post(reverse("activar_2fa"), {"codigo": "123456"})
        u.refresh_from_db()
        self.assertFalse(u.totp_activo)
        self.client.post(reverse("activar_2fa"), {"codigo": pyotp.TOTP(secret).now()})
        u.refresh_from_db()
        self.assertTrue(u.totp_activo)

    def test_admin_login_pasa_por_nuestro_login(self):
        resp = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(resp.url, reverse("login") + "?next=/admin/")


class LibroMayorTests(BaseTest):
    """Después de cualquier flujo, los saldos cacheados deben coincidir con el mayor."""

    def setUp(self):
        super().setUp()
        from django.core.management import call_command
        self.call_command = call_command
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()

    def assertConciliado(self):
        from django.db.models import Sum

        from .models import Asiento, LineaAsiento
        from .services.ledger import diferencias_de_conciliacion
        self.assertEqual(diferencias_de_conciliacion(Usuario.objects.all()), [])
        # cada asiento suma 0 por moneda
        desbalanceados = (LineaAsiento.objects.values('asiento_id', 'moneda')
                          .annotate(s=Sum('monto')).exclude(s=0))
        self.assertFalse(desbalanceados.exists())
        self.assertTrue(Asiento.objects.exists())

    def test_ciclo_completo(self):
        from .services import fondos, trading
        crear_cotizaciones()
        u = crear_usuario()

        dep = DepositoARS.objects.create(usuario=u, monto=Decimal("20000"), comprobante="c.png")
        fondos.aprobar_deposito_ars(dep.pk, self.admin)
        r = trading.cotizar(u, "compra", "USDT", Decimal("10200"), Cotizacion.objects.get(moneda="USDT"))
        trading.ejecutar_cotizacion(u, r.id)
        trading.swap(u, "USDT_to_USD", Decimal("5"), fee_bps=100)
        u.refresh_from_db()
        r = trading.cotizar(u, "venta", "USD", Decimal("2"), Cotizacion.objects.get(moneda="USD"))
        trading.ejecutar_cotizacion(u, r.id)

        r1 = fondos.solicitar_retiro_ars(u, Decimal("1000"), alias="mi.alias")
        fondos.aprobar_retiro_ars(r1.pk)
        fondos.enviar_retiro_ars(r1.pk, self.admin, titular_confirmado=True)
        r2 = fondos.solicitar_retiro_crypto(u, "USDT", Decimal("1"), "T1", red="TRC20")
        fondos.rechazar_retiro_crypto(r2.pk, self.admin, "Red equivocada")
        self.assertConciliado()

    def test_doble_aprobacion_no_acredita_dos_veces(self):
        from .services import fondos
        from .services.comun import OperacionError
        u = crear_usuario()
        dep = DepositoARS.objects.create(usuario=u, monto=Decimal("500"), comprobante="c.png")
        fondos.aprobar_deposito_ars(dep.pk, self.admin)
        with self.assertRaises(OperacionError):
            fondos.aprobar_deposito_ars(dep.pk, self.admin)
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("500"))

    def test_asiento_desbalanceado_se_rechaza(self):
        from .services.ledger import AsientoDesbalanceado, cuenta_usuario, registrar_asiento
        u = crear_usuario()
        with self.assertRaises(AsientoDesbalanceado):
            registrar_asiento('ajuste', 'mal', [(cuenta_usuario(u), 'ARS', Decimal("10"))])

    def test_asientos_inmutables(self):
        from django.core.exceptions import ValidationError

        from .services.ledger import cuenta_sistema, cuenta_usuario, registrar_asiento
        u = crear_usuario()
        a = registrar_asiento('ajuste', 'ok', [(cuenta_usuario(u), 'ARS', 1), (cuenta_sistema('ajustes'), 'ARS', -1)])
        with self.assertRaises(ValidationError):
            a.delete()
        with self.assertRaises(ValidationError):
            a.save()

    def test_apertura_para_saldos_previos_es_idempotente(self):
        crear_usuario(saldo_ars="1500", saldo_usdt="3")
        RetiroARS.objects.create(usuario=self.admin, alias="a", monto=Decimal("100"), estado="pendiente")
        self.call_command("ledger_apertura", verbosity=0)
        self.call_command("ledger_apertura", verbosity=0)
        self.assertConciliado()
        from .models import Asiento
        self.assertEqual(Asiento.objects.filter(referencia="apertura:retiros_pendientes").count(), 1)

    def test_conciliar_detecta_diferencias(self):
        u = crear_usuario()
        self.call_command("ledger_apertura", verbosity=0)
        Usuario.objects.filter(pk=u.pk).update(saldo_ars=Decimal("999"))  # saldo tocado por fuera
        with self.assertRaises(SystemExit):
            self.call_command("conciliar_saldos", stdout=open("/dev/null", "w"))

    def test_admin_marca_enviado_via_servicio(self):
        u = crear_usuario(saldo_ars="1000")
        self.call_command("ledger_apertura", verbosity=0)
        from .services import fondos
        r = fondos.solicitar_retiro_ars(u, Decimal("300"), alias="mi.alias")
        fondos.aprobar_retiro_ars(r.pk)
        self.admin.is_superuser = True  # el admin de Django exige permisos del modelo
        self.admin.save()
        self.client.force_login(self.admin)
        self.client.post(reverse("admin:usuarios_retiroars_changelist"),
                         {"action": "marcar_como_enviado", "_selected_action": [r.pk]})
        r.refresh_from_db()
        self.assertEqual(r.estado, "enviado")
        self.assertTrue(BoletoOperacion.objects.filter(usuario=u, tipo="retiro_ars").exists())
        self.assertConciliado()


class ConcurrenciaTests(TransactionTestCase):
    """Solo en PostgreSQL: SQLite serializa escrituras y no ejercita select_for_update."""

    def test_dos_retiros_simultaneos_no_gastan_dos_veces(self):
        import threading

        from django.db import close_old_connections, connection

        from .services import fondos
        from .services.comun import OperacionError

        if connection.vendor != 'postgresql':
            self.skipTest("requiere PostgreSQL")

        u = crear_usuario(saldo_ars="100")
        barrera = threading.Barrier(2)
        resultados = []

        def retirar():
            try:
                barrera.wait()
                fondos.solicitar_retiro_ars(u, Decimal("80"), alias="mi.alias")
                resultados.append("ok")
            except OperacionError:
                resultados.append("rechazado")
            finally:
                close_old_connections()

        hilos = [threading.Thread(target=retirar) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        u.refresh_from_db()
        self.assertEqual(sorted(resultados), ["ok", "rechazado"])
        self.assertEqual(u.saldo_ars, Decimal("20"))


class CotizacionReservadaTests(BaseTest):
    def setUp(self):
        super().setUp()
        crear_cotizaciones()
        self.u = crear_usuario(saldo_ars="20400")
        self.client.force_login(self.u)

    def _cotizar(self, monto="10200"):
        resp = self.client.post(reverse("operar"), {"operacion": "compra", "moneda": "USDT", "monto": monto})
        self.assertEqual(resp.status_code, 200)
        return resp.context["reserva"]

    def test_precio_bloqueado_aunque_cambie_la_cotizacion(self):
        reserva = self._cotizar()
        Cotizacion.objects.create(moneda="USDT", compra=1500, venta=2000, ref_compra=1700, ref_venta=1700)
        self.client.post(reverse("confirmar_operacion"), {"reserva": reserva.id})
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_usdt, Decimal("10"))  # al precio reservado (1020), no a 2000

    def test_reserva_expirada(self):
        from .models import CotizacionReservada
        reserva = self._cotizar()
        CotizacionReservada.objects.filter(pk=reserva.pk).update(expira=timezone.now() - timedelta(seconds=1))
        self.client.post(reverse("confirmar_operacion"), {"reserva": reserva.id})
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_usdt, Decimal("0"))

    def test_reserva_no_se_reutiliza(self):
        reserva = self._cotizar()
        self.client.post(reverse("confirmar_operacion"), {"reserva": reserva.id})
        self.client.post(reverse("confirmar_operacion"), {"reserva": reserva.id})
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_usdt, Decimal("10"))

    def test_reserva_de_otro_usuario(self):
        reserva = self._cotizar()
        otro = crear_usuario("otro", saldo_ars="20400")
        self.client.force_login(otro)
        self.client.post(reverse("confirmar_operacion"), {"reserva": reserva.id})
        otro.refresh_from_db()
        self.assertEqual(otro.saldo_usdt, Decimal("0"))

    def test_reserva_invalida_no_rompe(self):
        resp = self.client.post(reverse("confirmar_operacion"), {"reserva": "no-es-un-uuid"})
        self.assertEqual(resp.status_code, 302)


class ComplianceTests(BaseTest):
    def setUp(self):
        super().setUp()
        from .models import ExchangeConfig
        crear_cotizaciones()
        cfg = ExchangeConfig.current()
        cfg.umbral_alerta_ars = Decimal("100000")
        cfg.limite_mensual_operado_ars = Decimal("500000")
        cfg.limite_diario_retiro_ars = Decimal("200000")
        cfg.save()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()

    def alertas(self, tipo):
        from .models import AlertaCompliance
        return AlertaCompliance.objects.filter(tipo=tipo)

    def test_operacion_sobre_umbral_genera_alerta(self):
        u = crear_usuario(saldo_ars="200000")
        self.client.force_login(u)
        operar(self.client, "compra", "USDT", "150000")
        self.assertEqual(self.alertas("umbral").count(), 1)

    def test_fraccionamiento(self):
        u = crear_usuario(saldo_ars="200000")
        self.client.force_login(u)
        for _ in range(3):
            operar(self.client, "compra", "USDT", "40000")  # cada una bajo el umbral, juntas lo superan
        self.assertEqual(self.alertas("fraccionamiento").count(), 1)
        self.assertEqual(self.alertas("umbral").count(), 0)

    def test_limite_mensual_bloquea_y_deja_alerta(self):
        u = crear_usuario(saldo_ars="900000")
        self.client.force_login(u)
        operar(self.client, "compra", "USDT", "600000")
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("900000"))
        self.assertEqual(self.alertas("limite_excedido").count(), 1)  # sobrevive al rollback

    def test_limite_propio_del_usuario(self):
        u = crear_usuario(saldo_ars="900000")
        u.limite_mensual_ars = Decimal("1000000")
        u.save()
        self.client.force_login(u)
        operar(self.client, "compra", "USDT", "600000")
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("300000"))

    def test_limite_diario_de_retiro(self):
        u = crear_usuario(saldo_ars="500000")
        self.client.force_login(u)
        self.client.post(reverse("solicitar_retiro"), {"monto": "150000", "alias": "mi.alias"})
        self.client.post(reverse("solicitar_retiro"), {"monto": "100000", "alias": "mi.alias"})  # 250k > 200k
        self.assertEqual(RetiroARS.objects.count(), 1)
        self.assertEqual(self.alertas("limite_excedido").count(), 1)

    def test_retiro_rapido_tras_deposito(self):
        from .services import fondos
        u = crear_usuario()
        dep = DepositoARS.objects.create(usuario=u, monto=Decimal("50000"), comprobante="c.png")
        fondos.aprobar_deposito_ars(dep.pk, self.admin)
        fondos.solicitar_retiro_ars(u, Decimal("49000"), alias="mi.alias")
        self.assertEqual(self.alertas("retiro_rapido").count(), 1)

    def test_resolver_alerta_exige_nota_y_audita(self):
        from .models import AlertaCompliance, RegistroAuditoria
        u = crear_usuario()
        a = AlertaCompliance.objects.create(usuario=u, tipo="umbral", detalle="x", monto_ars=1)
        self.client.force_login(self.admin)
        self.client.post(reverse("resolver_alerta", args=[a.id]), {"estado": "descartada", "nota": ""})
        a.refresh_from_db()
        self.assertEqual(a.estado, "abierta")
        self.client.post(reverse("resolver_alerta", args=[a.id]), {"estado": "descartada", "nota": "Cliente conocido"})
        a.refresh_from_db()
        self.assertEqual((a.estado, a.revisada_por), ("descartada", self.admin))
        self.assertTrue(RegistroAuditoria.objects.filter(accion="alerta_descartada").exists())

    def test_paginas_admin(self):
        self.client.force_login(self.admin)
        for nombre in ("compliance_alertas", "auditoria", "panel_admin", "panel_depositos_usdt"):
            self.assertEqual(self.client.get(reverse(nombre)).status_code, 200, nombre)
        self.client.force_login(crear_usuario("comun"))
        self.assertEqual(self.client.get(reverse("compliance_alertas")).status_code, 302)


def _respuesta(json_data):
    from unittest import mock
    r = mock.Mock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


WALLET_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # cualquier dirección TRON válida sirve para el test
WALLET_ERC20 = "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36"


@override_settings(WALLETS_EMPRESA={"TRC20": WALLET_TRC20, "ERC20": WALLET_ERC20})
class OnchainTests(BaseTest):
    TX = "ab" * 32

    def _tron(self, *, destino_hex, monto_crudo, bloque=100, actual=200, resultado="SUCCESS"):
        from .services.onchain import TRANSFER_TOPIC, tron_a_hex
        info = {
            "blockNumber": bloque, "receipt": {"result": resultado},
            "log": [{"address": tron_a_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),
                     "topics": [TRANSFER_TOPIC, "0" * 24 + "11" * 20, "0" * 24 + destino_hex],
                     "data": f"{monto_crudo:064x}"}],
        }
        ahora = {"block_header": {"raw_data": {"number": actual}}}
        return [_respuesta(info), _respuesta(ahora)]

    def verificar(self, red, monto, respuestas):
        from unittest import mock

        from .services import onchain
        with mock.patch("usuarios.services.onchain.requests.post", side_effect=respuestas):
            return onchain.verificar(red, self.TX, Decimal(monto))

    def test_tron_hex(self):
        from .services.onchain import tron_a_hex
        self.assertEqual(tron_a_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"), "a614f803b6fd780986a42c78ec9c7f77e6ded13c")
        with self.assertRaises(ValueError):
            tron_a_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6x")  # checksum inválido

    def test_trc20_ok(self):
        from .services.onchain import tron_a_hex
        r = self.verificar("TRC20", "50", self._tron(destino_hex=tron_a_hex(WALLET_TRC20), monto_crudo=50_000_000))
        self.assertEqual((r.estado, r.monto), ("verificado", Decimal("50")))

    def test_trc20_otra_wallet(self):
        r = self.verificar("TRC20", "50", self._tron(destino_hex="22" * 20, monto_crudo=50_000_000))
        self.assertEqual(r.estado, "no_coincide")

    def test_trc20_monto_menor(self):
        from .services.onchain import tron_a_hex
        r = self.verificar("TRC20", "50", self._tron(destino_hex=tron_a_hex(WALLET_TRC20), monto_crudo=10_000_000))
        self.assertEqual(r.estado, "no_coincide")

    def test_trc20_pocas_confirmaciones(self):
        from .services.onchain import tron_a_hex
        r = self.verificar("TRC20", "50", self._tron(destino_hex=tron_a_hex(WALLET_TRC20), monto_crudo=50_000_000,
                                                     bloque=195, actual=200))
        self.assertEqual(r.estado, "pendiente")

    def test_trc20_fallida(self):
        from .services.onchain import tron_a_hex
        r = self.verificar("TRC20", "50", self._tron(destino_hex=tron_a_hex(WALLET_TRC20), monto_crudo=50_000_000,
                                                     resultado="REVERT"))
        self.assertEqual(r.estado, "no_coincide")

    def test_erc20_ok_y_contrato_falso(self):
        from .services.onchain import TRANSFER_TOPIC

        def recibo(contrato):
            return _respuesta({"result": {"status": "0x1", "blockNumber": hex(100), "logs": [
                {"address": contrato, "topics": ["0x" + TRANSFER_TOPIC, "0x" + "0" * 64, "0x" + "0" * 24 + WALLET_ERC20[2:]],
                 "data": hex(25_000_000)}]}})
        bloque = _respuesta({"result": hex(150)})
        r = self.verificar("ERC20", "25", [recibo("0xdAC17F958D2ee523a2206206994597C13D831ec7"), bloque])
        self.assertEqual(r.estado, "verificado")
        # mismo evento Transfer pero de un token falso que imita a USDT
        r = self.verificar("ERC20", "25", [recibo("0x" + "99" * 20), bloque])
        self.assertEqual(r.estado, "no_coincide")

    def test_error_de_red(self):
        import requests
        r = self.verificar("TRC20", "50", requests.ConnectionError("caído"))
        self.assertEqual(r.estado, "error")

    def test_red_no_soportada(self):
        from .services import onchain
        self.assertEqual(onchain.verificar("SOL", self.TX, Decimal("1")).estado, "no_soportada")


class DepositoUSDTAprobacionTests(BaseTest):
    def setUp(self):
        super().setUp()
        from .models import DepositoUSDT
        self.u = crear_usuario()
        self.dep = DepositoUSDT.objects.create(usuario=self.u, monto=Decimal("50"), red="TRC20",
                                               txid="ab" * 32, comprobante="c.png")
        self.staff = crear_usuario("staff")
        self.staff.is_staff, self.staff.rol_staff = True, "superadmin"
        self.staff.save()
        self.superu = crear_usuario("root")
        self.superu.is_staff = self.superu.is_superuser = True
        self.superu.save()

    def _aprobar(self, quien, forzar=False):
        self.client.force_login(quien)
        data = {"forzar": "1"} if forzar else {}
        self.client.post(reverse("aprobar_deposito_usdt", args=[self.dep.id]), data)
        self.u.refresh_from_db()
        return self.u.saldo_usdt

    def test_sin_verificar_no_se_aprueba(self):
        self.assertEqual(self._aprobar(self.staff), Decimal("0"))

    def test_staff_no_puede_forzar(self):
        self.assertEqual(self._aprobar(self.staff, forzar=True), Decimal("0"))

    def test_superusuario_fuerza_y_queda_auditado(self):
        from .models import RegistroAuditoria
        self.assertEqual(self._aprobar(self.superu, forzar=True), Decimal("50"))
        reg = RegistroAuditoria.objects.get(accion="deposito_usdt_forzado")
        self.assertEqual((reg.actor, reg.ip), (self.superu, "127.0.0.1"))

    def test_no_coincide_nunca_se_aprueba(self):
        self.dep.verificacion_estado = "no_coincide"
        self.dep.save()
        self.assertEqual(self._aprobar(self.superu, forzar=True), Decimal("0"))

    def test_verificado_se_aprueba(self):
        self.dep.verificacion_estado = "verificado"
        self.dep.save()
        self.assertEqual(self._aprobar(self.staff), Decimal("50"))


class AuditoriaTests(BaseTest):
    def test_login_y_kyc_quedan_registrados(self):
        from .models import RegistroAuditoria
        crear_usuario()
        self.client.post(reverse("login"), {"username": "cliente", "password": "Clave-Segura-123"})
        self.assertTrue(RegistroAuditoria.objects.filter(accion="login").exists())

        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        objetivo = crear_usuario("nuevo", aprobado=False)
        crear_declaracion(objetivo)
        self.client.force_login(admin)
        self.client.post(reverse("cambiar_estado_verificacion", args=[objetivo.id]), {"estado": "aprobado"})
        reg = RegistroAuditoria.objects.get(accion="kyc_cambio_estado")
        self.assertEqual(reg.detalle, {"anterior": "pendiente", "nuevo": "aprobado"})

    def test_registro_inmutable(self):
        from django.core.exceptions import ValidationError

        from .services.auditoria import auditar
        r = auditar(None, "prueba")
        with self.assertRaises(ValidationError):
            r.delete()


class DemoYCotizacionesTests(BaseTest):
    def test_respaldo_binance_spot_si_p2p_falla(self):
        from unittest import mock

        import requests

        from .services import cotizaciones
        spot = _respuesta({"bidPrice": "1600.00", "askPrice": "1602.00"})
        with mock.patch("usuarios.services.cotizaciones.requests.post", side_effect=requests.ConnectionError("bloqueado")), \
             mock.patch("usuarios.services.cotizaciones.requests.get", return_value=spot):
            cot, fuente = cotizaciones.actualizar("USDT")
        self.assertEqual((fuente, cot.ref_venta), ("Binance spot", Decimal("1601")))

    def test_respaldo_dolar_cripto_si_binance_no_responde(self):
        from unittest import mock

        import requests

        from .services import cotizaciones

        def get(url, **kw):
            if "binance" in url:
                raise requests.HTTPError("451 Client Error")
            return _respuesta({"compra": 1640, "venta": 1650})
        with mock.patch("usuarios.services.cotizaciones.requests.post", side_effect=requests.ConnectionError("bloqueado")), \
             mock.patch("usuarios.services.cotizaciones.requests.get", side_effect=get):
            cot, fuente = cotizaciones.actualizar("USDT")
        self.assertEqual((fuente, cot.ref_venta), ("DolarAPI cripto", Decimal("1645")))

    @override_settings(COTIZACION_ACTUALIZAR_AL_VUELO=True)
    def test_operar_actualiza_cotizacion_vencida(self):
        from unittest import mock
        crear_cotizaciones(edad=timedelta(hours=2))
        self.client.force_login(crear_usuario())

        def nueva(moneda):
            return Cotizacion.objects.create(moneda=moneda, compra=980, venta=1020, ref_compra=1000, ref_venta=1000), "test"
        with mock.patch("usuarios.services.cotizaciones.actualizar", side_effect=nueva):
            resp = self.client.get(reverse("operar"))
        self.assertTrue(resp.context["cotizaciones_vigentes"])

    def test_pdf_fallido_bloquea_salvo_en_demo(self):
        from unittest import mock

        from .services import fondos
        admin = crear_usuario("admin")
        u = crear_usuario()
        roto = mock.patch("usuarios.services.boletos._get_weasyprint_HTML", side_effect=OSError("sin pango"))
        dep = DepositoARS.objects.create(usuario=u, monto=Decimal("100"), comprobante="c.png")
        with roto, self.assertRaises(OSError):
            fondos.aprobar_deposito_ars(dep.pk, admin)
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("0"))
        with roto, override_settings(BOLETOS_TOLERAR_ERROR_PDF=True):
            fondos.aprobar_deposito_ars(dep.pk, admin)
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("100"))
        # el comprobante queda igual, sin PDF: se ve en pantalla y se descarga como imagen
        b = BoletoOperacion.objects.get(usuario=u)
        self.assertFalse(b.pdf)
        self.client.force_login(u)
        self.assertContains(self.client.get(reverse("ver_comprobante", args=[b.numero])), "$ 100,00")
        resp = self.client.get(reverse("descargar_boleto", args=[b.numero]))
        self.assertRedirects(resp, reverse("ver_comprobante", args=[b.numero]), fetch_redirect_response=False)

    def test_cargar_demo_solo_en_modo_demo(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("cargar_demo")

    @override_settings(DEMO_MODE=True)
    def test_cartel_demo(self):
        self.assertContains(self.client.get(reverse("login")), "Entorno de demostración")


@override_settings(MEDIA_ROOT=MEDIA_TMP, SECURE_SSL_REDIRECT=False)
class ActividadTests(BaseTest):
    """Movimientos mostrados como actividades legibles (una compra = una fila)."""

    def setUp(self):
        super().setUp()
        crear_cotizaciones()
        self.u = crear_usuario(saldo_ars="200000", saldo_usd="50")
        self.client.force_login(self.u)

    def _agrupar(self):
        from .services import presentacion
        return presentacion.agrupar(Movimiento.objects.filter(usuario=self.u))

    def test_compra_y_swap_se_agrupan_con_texto_legible(self):
        from .services import trading
        operar(self.client, "compra", "USDT", "102000")
        trading.swap(self.u, "USD_to_USDT", Decimal("10"), fee_bps=100)
        swap, compra = self._agrupar()
        self.assertEqual((compra.tipo, compra.titulo, compra.moneda), ("compra", "Compraste USDT", "USDT"))
        self.assertEqual(compra.monto, Decimal("100.00"))
        self.assertIn("Pagaste $ 102.000,00", compra.detalle)
        self.assertIsNotNone(compra.boleto)
        self.assertEqual((swap.tipo, swap.titulo), ("swap", "Convertiste dólares a USDT"))
        self.assertEqual(swap.monto, Decimal("9.90"))
        self.assertEqual(len(swap.movimientos), 2)

    def test_deposito_informado_queda_en_revision(self):
        from .utils import registrar_movimiento
        registrar_movimiento(self.u, "deposito", "ARS", 0,
                             "Solicitud de depósito ARS enviada (monto: $15000.00). En revisión.")
        (a,) = self._agrupar()
        self.assertEqual((a.titulo, a.estado, a.detalle), ("Informaste un depósito en pesos", "En revisión", "Por $ 15.000,00"))
        self.assertIsNone(a.monto)

    def test_migracion_empareja_operaciones_previas(self):
        import importlib
        from django.apps import apps
        from .utils import registrar_movimiento
        registrar_movimiento(self.u, "compra", "ARS", Decimal("-1020"), "Compra de USDT a $1020")
        registrar_movimiento(self.u, "compra", "USDT", Decimal("1"), "Compra de USDT con $1020 ARS")
        registrar_movimiento(self.u, "deposito", "ARS", Decimal("500"), "Depósito aprobado")
        mig = importlib.import_module("usuarios.migrations.0024_movimiento_operacion")
        mig.agrupar_existentes(apps, None)
        ops = set(Movimiento.objects.filter(tipo="compra").values_list("operacion", flat=True))
        self.assertEqual(len(ops), 1)
        self.assertIsNotNone(ops.pop())
        self.assertIsNone(Movimiento.objects.get(tipo="deposito").operacion)

    def test_filtros_y_csv(self):
        from .services import trading
        operar(self.client, "compra", "USDT", "102000")
        trading.swap(self.u, "USD_to_USDT", Decimal("10"), fee_bps=100)
        resp = self.client.get(reverse("mis_movimientos"), {"tipo": "swap"})
        self.assertEqual([a.tipo for a in resp.context["page_obj"]], ["swap"])
        self.assertContains(resp, "Convertiste dólares a USDT")
        resp = self.client.get(reverse("mis_movimientos"), {"moneda": "ARS"})
        self.assertEqual([a.tipo for a in resp.context["page_obj"]], ["compra"])
        csv = self.client.get(reverse("movimientos_exportar"), {"tipo": "compra"}).content.decode()
        self.assertEqual(len(csv.strip().splitlines()), 3)  # encabezado + las dos patas de la compra


@override_settings(MEDIA_ROOT=MEDIA_TMP, SECURE_SSL_REDIRECT=False)
class PanelPendientesTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        self.client.force_login(self.admin)
        self.cliente = crear_usuario()
        self.nuevo = crear_usuario("nuevo", aprobado=False)
        crear_declaracion(self.nuevo)
        self.dep = DepositoARS.objects.create(usuario=self.cliente, monto=Decimal("1500"), comprobante="c.png")

    def test_cola_ordenada_y_filtrable(self):
        resp = self.client.get(reverse("panel_admin"))
        fechas = [c["fecha"] for c in resp.context["cola"]]
        self.assertEqual(fechas, sorted(fechas))  # del más viejo al más nuevo
        tipos = {c["tipo"] for c in resp.context["cola"]}
        self.assertTrue({"kyc", "deposito_ars"} <= tipos)
        self.assertEqual(resp.context["pendientes"]["depositos_ars"], 1)
        resp = self.client.get(reverse("panel_admin"), {"tipo": "deposito_ars"})
        self.assertEqual([c["obj"] for c in resp.context["cola"]], [self.dep])

    def test_acreditar_desde_la_cola_vuelve_a_la_cola(self):
        destino = reverse("panel_admin") + "?tipo=deposito_ars"
        resp = self.client.post(reverse("aprobar_deposito", args=[self.dep.pk]), {"next": destino})
        self.assertRedirects(resp, destino, fetch_redirect_response=False)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.saldo_ars, Decimal("1500"))

    def test_next_externo_se_ignora(self):
        resp = self.client.post(reverse("cambiar_estado_verificacion", args=[self.nuevo.pk]),
                                {"estado": "aprobado", "next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("panel_admin"), fetch_redirect_response=False)
        self.nuevo.refresh_from_db()
        self.assertEqual(self.nuevo.estado_verificacion, "aprobado")

    def test_busqueda_va_al_listado_de_usuarios(self):
        resp = self.client.get(reverse("panel_admin"), {"q": "nuevo"})
        self.assertRedirects(resp, reverse("admin_usuarios_list") + "?q=nuevo", fetch_redirect_response=False)

    def test_parametros(self):
        self.assertContains(self.client.get(reverse("parametros_exchange")), "Guardar parámetros")
        resp = self.client.post(reverse("guardar_config_exchange"), {
            "spread_bps_usdt": 150, "spread_bps_usd": 200, "swap_fee_bps": 50,
            "umbral_alerta_ars": "1000000", "limite_mensual_operado_ars": "5000000", "limite_diario_retiro_ars": "2000000",
        })
        self.assertRedirects(resp, reverse("parametros_exchange"), fetch_redirect_response=False)
        from .models import ExchangeConfig
        self.assertEqual(ExchangeConfig.objects.get().spread_bps_usdt, 150)


class AdminMovimientosTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        self.client.force_login(self.admin)
        self.ana = crear_usuario("ana")
        self.beto = crear_usuario("beto")
        mov = lambda u, tipo, moneda, monto: Movimiento.objects.create(  # noqa: E731
            usuario=u, tipo=tipo, moneda=moneda, monto=Decimal(monto), saldo_antes=0, saldo_despues=0)
        self.dep_grande = mov(self.ana, "deposito", "ARS", "500000")
        self.compra_ars = mov(self.ana, "compra", "ARS", "-200000")
        self.compra_usdt = mov(self.ana, "compra", "USDT", "120")
        self.dep_beto = mov(self.beto, "deposito", "ARS", "1000")
        viejo = timezone.now() - timedelta(days=40)
        Movimiento.objects.filter(pk=self.dep_beto.pk).update(fecha=viejo)

    def listado(self, **filtros):
        resp = self.client.get(reverse("admin_movimientos"), filtros)
        self.assertEqual(resp.status_code, 200)
        return resp, set(resp.context["page_obj"].object_list)

    def test_filtros(self):
        _, todos = self.listado()
        self.assertEqual(len(todos), 4)
        self.assertEqual(self.listado(usuario="ana")[1], {self.dep_grande, self.compra_ars, self.compra_usdt})
        self.assertEqual(self.listado(u=self.beto.pk)[1], {self.dep_beto})
        self.assertEqual(self.listado(moneda="USDT")[1], {self.compra_usdt})
        self.assertEqual(self.listado(tipo="compra", sentido="salida")[1], {self.compra_ars})
        # el monto se compara sin signo
        self.assertEqual(self.listado(min="150000")[1], {self.dep_grande, self.compra_ars})
        self.assertEqual(self.listado(min="100", max="300000")[1], {self.compra_ars, self.compra_usdt, self.dep_beto})
        hace_una_semana = (timezone.localdate() - timedelta(days=7)).isoformat()
        self.assertNotIn(self.dep_beto, self.listado(desde=hace_una_semana)[1])

    def test_totales_por_moneda(self):
        resp, _ = self.listado(usuario="ana", moneda="ARS")
        (t,) = resp.context["totales"]
        self.assertEqual((t["entradas"], t["salidas"], t["neto"]),
                         (Decimal("500000"), Decimal("200000"), Decimal("300000")))

    def test_csv_respeta_filtros(self):
        resp = self.client.get(reverse("exportar_movimientos_admin"), {"usuario": "ana", "moneda": "ARS"})
        filas = resp.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(filas), 3)  # encabezado + 2
        self.assertIn("Usuario", filas[0])
        self.assertTrue(all(",ana," in f for f in filas[1:]))

    def test_solo_staff(self):
        self.client.force_login(self.ana)
        self.assertEqual(self.client.get(reverse("admin_movimientos")).status_code, 302)
        self.assertEqual(self.client.get(reverse("exportar_movimientos_admin")).status_code, 302)


class AdminCotizacionesTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        self.client.force_login(self.admin)
        crear_cotizaciones(edad=timedelta(hours=1))

    def fijar(self, **datos):
        datos = {"moneda": "USDT", "compra": "1500", "venta": "1560", "duracion": "60", **datos}
        return self.client.post(reverse("fijar_precio_manual"), datos)

    def test_pantalla(self):
        resp = self.client.get(reverse("admin_cotizaciones"))
        self.assertContains(resp, "Fijar un precio manual")
        self.assertEqual(len(resp.context["monedas"]), 2)

    def test_precio_manual_no_lo_pisa_la_actualizacion(self):
        from .services import cotizaciones
        self.fijar()
        cot = cotizaciones.ultima("USDT")
        self.assertTrue(cot.manual_activa)
        self.assertEqual((cot.compra, cot.venta, cot.ref_venta), (Decimal("1500"), Decimal("1560"), Decimal("1530")))
        self.assertEqual(cot.margin_bps, 196)
        with self.assertRaises(cotizaciones.PrecioManualActivo):
            cotizaciones.actualizar("USDT")
        self.assertTrue(RegistroAuditoria.objects.filter(accion="cotizacion_manual").exists())

    def test_precio_manual_no_vence_y_se_usa_para_operar(self):
        from .services import cotizaciones
        from .utils import cotizacion_vigente
        self.fijar(duracion="0")
        self.fijar(moneda="USD", compra="880", venta="920", duracion="0")
        # horas después: las de mercado y las manuales ya pasaron los 15 minutos
        Cotizacion.objects.filter(manual=False).update(fecha=timezone.now() - timedelta(hours=6))
        Cotizacion.objects.filter(manual=True).update(fecha=timezone.now() - timedelta(hours=5))
        cot = cotizaciones.ultima("USDT")
        self.assertTrue(cot.manual and cot.manual_hasta is None)
        self.assertTrue(cotizacion_vigente(cot))
        self.client.force_login(crear_usuario("cli"))
        self.assertTrue(self.client.get(reverse("operar")).context["cotizaciones_vigentes"])

    def test_precio_manual_vencido_vuelve_al_mercado(self):
        from .services import cotizaciones
        from .utils import cotizacion_vigente
        self.fijar(duracion="30")
        cot = Cotizacion.objects.latest("id")
        Cotizacion.objects.filter(pk=cot.pk).update(fecha=timezone.now() - timedelta(minutes=40),
                                                    manual_hasta=timezone.now() - timedelta(minutes=10))
        cot.refresh_from_db()
        self.assertFalse(cot.manual_activa)
        self.assertFalse(cotizacion_vigente(cot))
        with mock_fuente(cotizaciones, 1600):
            _, fuente = cotizaciones.actualizar("USDT")
        self.assertEqual(fuente, "test")

    def test_quitar_precio_manual(self):
        from .services import cotizaciones
        self.fijar(duracion="0")
        with mock_fuente(cotizaciones, 1600):
            self.client.post(reverse("quitar_precio_manual"), {"moneda": "USDT"})
        cot = cotizaciones.ultima("USDT")
        self.assertFalse(cot.manual)
        self.assertEqual(cot.ref_venta, Decimal("1600"))
        self.assertTrue(RegistroAuditoria.objects.filter(accion="cotizacion_manual_quitada").exists())

    def test_precio_manual_por_una_semana(self):
        from .services import cotizaciones
        self.fijar(duracion="10080")
        cot = cotizaciones.ultima("USDT")
        self.assertAlmostEqual((cot.manual_hasta - timezone.now()).total_seconds(), 7 * 24 * 3600, delta=60)
        self.assertContains(self.client.get(reverse("admin_cotizaciones")), "1 semana")

    def test_mismo_precio_para_compra_y_venta(self):
        from .services import cotizaciones
        self.fijar(compra="1600", venta="1600")
        cot = cotizaciones.ultima("USDT")
        self.assertTrue(cot.manual_activa)
        self.assertEqual((cot.compra, cot.venta, cot.margin_bps), (Decimal("1600"), Decimal("1600"), 0))

    def test_validaciones(self):
        antes = Cotizacion.objects.count()
        self.fijar(compra="1600", venta="1500")
        self.fijar(compra="abc")
        self.fijar(moneda="BTC")
        self.fijar(duracion="99999")
        self.fijar(duracion="20160")
        self.assertEqual(Cotizacion.objects.count(), antes)

    def test_solo_staff(self):
        cliente = crear_usuario("cli")
        self.client.force_login(cliente)
        self.assertEqual(self.client.get(reverse("admin_cotizaciones")).status_code, 302)
        self.fijar()
        self.assertFalse(Cotizacion.objects.filter(manual=True).exists())


def mock_fuente(cotizaciones, precio):
    """Reemplaza las fuentes de USDT por una que devuelve `precio`."""
    from unittest import mock
    return mock.patch.dict(cotizaciones.FUENTES, {"USDT": (lambda: (Decimal(precio), "test"),)})


class ContabilidadResumenTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        self.client.force_login(self.admin)
        crear_cotizaciones()
        self.cli = crear_usuario()

    def resumen(self, **params):
        resp = self.client.get(reverse("exchange_dashboard"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.context["resumen"]

    def test_ingresado_retirado_y_custodia(self):
        from .services import fondos
        with self.captureOnCommitCallbacks(execute=True):
            dep = DepositoARS.objects.create(usuario=self.cli, monto=Decimal("100000"), comprobante="c.png")
            fondos.aprobar_deposito_ars(dep.pk, self.admin)
        r = self.resumen()
        self.assertEqual(r["ingresado"]["ars"], Decimal("100000"))
        self.assertEqual(r["ingresado"]["n"], 1)
        self.assertEqual(r["retirado"]["ars"], 0)
        self.assertEqual(r["neto_ars"], Decimal("100000"))
        custodia = {c["moneda"]: c["total"] for c in r["custodia"]}
        self.assertEqual(custodia["ARS"], Decimal("100000"))
        self.assertEqual(sum(r["serie"]["entradas"]), 100000)

        # un período sin movimientos no suma, pero la custodia sigue siendo la actual
        r = self.resumen(desde="2020-01-01", hasta="2020-01-31")
        self.assertEqual(r["ingresado"]["ars"], 0)
        self.assertEqual({c["moneda"]: c["total"] for c in r["custodia"]}["ARS"], Decimal("100000"))

    def test_pantalla(self):
        resp = self.client.get(reverse("exchange_dashboard"))
        self.assertContains(resp, "Dinero ingresado")
        self.assertContains(resp, "En custodia ahora")


@override_settings(BOLETOS_TOLERAR_ERROR_PDF=True)
class ComprobantesTests(BaseTest):
    def setUp(self):
        super().setUp()
        from unittest import mock
        crear_cotizaciones()
        self.cli = crear_usuario(saldo_ars=Decimal("500000"))
        self.client.force_login(self.cli)
        with mock.patch("usuarios.services.boletos._get_weasyprint_HTML", side_effect=OSError("sin pango")):
            operar(self.client, "compra", "USDT", "100000")
        self.b = BoletoOperacion.objects.get(usuario=self.cli)

    def test_cliente_ve_su_comprobante_con_montos(self):
        resp = self.client.get(reverse("ver_comprobante", args=[self.b.numero]))
        self.assertContains(resp, "Compra de USDT")
        self.assertContains(resp, "$ 100.000,00")  # pagó
        self.assertContains(resp, "1.020,00 ARS / USDT")  # cotización
        self.assertContains(resp, "Descargar imagen")
        self.assertContains(self.client.get(reverse("comprobantes")), self.b.numero)

    def test_otro_cliente_no_lo_ve(self):
        self.client.force_login(crear_usuario("otro"))
        self.assertEqual(self.client.get(reverse("ver_comprobante", args=[self.b.numero])).status_code, 404)
        self.assertEqual(self.client.get(reverse("descargar_boleto", args=[self.b.numero])).status_code, 404)

    def test_admin_lo_ve_en_el_perfil_y_en_movimientos(self):
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        self.client.force_login(admin)
        url = reverse("ver_comprobante", args=[self.b.numero])
        self.assertContains(self.client.get(reverse("admin_usuario_perfil", args=[self.cli.pk])), url)
        self.assertEqual(self.client.get(url).status_code, 200)
        # las dos patas de la compra (pesos que salen y USDT que entran) muestran el comprobante
        resp = self.client.get(reverse("admin_movimientos"), {"u": self.cli.pk, "tipo": "compra"})
        filas = list(resp.context["page_obj"])
        self.assertEqual(len(filas), 2)
        self.assertTrue(all(m.boleto == self.b for m in filas))


def _png(nombre="c.png"):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
    return SimpleUploadedFile(nombre, buf.getvalue(), content_type="image/png")


@override_settings(BOLETOS_TOLERAR_ERROR_PDF=True, CUENTAS_DEPOSITO={
    "ARS": {"banco": "B", "titular": "T", "cuit": "1", "cbu": "111", "alias": "pesos.alias"},
    "USD": {"banco": "B", "titular": "T", "cuit": "1", "cbu": "222", "alias": "dolares.alias"},
})
class DepositoDolaresTests(BaseTest):
    def setUp(self):
        super().setUp()
        crear_cotizaciones()
        self.cli = crear_usuario()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()

    def informar(self, monto="250"):
        self.client.force_login(self.cli)
        return self.client.post(reverse("depositar_usd"), {"monto": monto, "comprobante": _png()})

    def test_pantalla_muestra_la_cuenta_en_dolares(self):
        self.client.force_login(self.cli)
        resp = self.client.get(reverse("depositar_usd"))
        self.assertContains(resp, "dolares.alias")
        self.assertNotContains(resp, "pesos.alias")
        self.assertContains(self.client.get(reverse("agregar_saldo")), "pesos.alias")

    def test_deposito_en_dolares_se_acredita_en_dolares(self):
        from unittest import mock
        self.assertRedirects(self.informar(), reverse("dashboard"), fetch_redirect_response=False)
        dep = DepositoARS.objects.get(usuario=self.cli)
        self.assertEqual((dep.moneda, dep.monto, dep.estado), ("USD", Decimal("250"), "pendiente"))

        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("panel_admin")), "Depósito en dólares")
        with mock.patch("usuarios.services.boletos._get_weasyprint_HTML", side_effect=OSError("sin pango")), \
             self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("aprobar_deposito", args=[dep.pk]))
        self.cli.refresh_from_db()
        self.assertEqual((self.cli.saldo_usd, self.cli.saldo_ars), (Decimal("250"), Decimal("0")))
        self.assertEqual(BoletoOperacion.objects.get(usuario=self.cli).tipo, "deposito_usd")
        self.assertTrue(ApunteExchange.objects.filter(categoria="entrada", moneda="USD").exists())

    def test_rechazo_en_dolares(self):
        self.informar()
        dep = DepositoARS.objects.get(usuario=self.cli)
        self.client.force_login(self.admin)
        self.client.post(reverse("rechazar_deposito", args=[dep.pk]))
        dep.refresh_from_db()
        self.assertEqual(dep.estado, "rechazado")
        self.assertTrue(Movimiento.objects.filter(usuario=self.cli, tipo="ajuste", moneda="USD").exists())

    def test_usdt_solo_ofrece_redes_con_billetera(self):
        self.client.force_login(self.cli)
        with self.settings(WALLETS_EMPRESA={"TRC20": "Twallet", "ERC20": "", "BEP20": "", "SOL": ""}):
            resp = self.client.get(reverse("depositar_usdt"))
        self.assertContains(resp, "Twallet")
        self.assertEqual([v for v, _ in resp.context["form"].fields["red"].choices], ["TRC20"])


@override_settings(SOPORTE_TO=["soporte@test.local"])
class SoporteTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.cli = crear_usuario()
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()

    def abrir(self):
        self.client.force_login(self.cli)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("soporte"), {
                "asunto": "No me llegó el retiro", "categoria": "pagos", "prioridad": "alta",
                "mensaje": "Pedí un retiro ayer", "email": "cli@test.local",
            })

    def test_cliente_abre_ticket_y_avisa_al_equipo(self):
        from django.core import mail
        resp = self.abrir()
        t = SupportTicket.objects.get()
        self.assertRedirects(resp, reverse("ver_ticket", args=[t.pk]), fetch_redirect_response=False)
        self.assertEqual(t.estado, "abierto")
        self.assertEqual({tuple(m.to) for m in mail.outbox}, {("soporte@test.local",), ("cli@test.local",)})
        self.assertContains(self.client.get(reverse("mis_tickets")), "No me llegó el retiro")

    def test_conversacion_y_estados(self):
        from django.core import mail
        self.abrir()
        t = SupportTicket.objects.get()
        mail.outbox.clear()

        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("admin_tickets")), "No me llegó el retiro")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin_ticket", args=[t.pk]), {"mensaje": "Ya salió, revisá en 1 hora"})
        t.refresh_from_db()
        self.assertEqual(t.estado, "respondido")
        self.assertEqual(mail.outbox[-1].to, ["cli@test.local"])
        self.assertTrue(Notificacion.objects.filter(usuario=self.cli).exists())

        # el cliente responde: vuelve a abierto
        self.client.force_login(self.cli)
        self.client.post(reverse("ver_ticket", args=[t.pk]), {"mensaje": "Gracias, llegó"})
        t.refresh_from_db()
        self.assertEqual(t.estado, "abierto")
        self.assertEqual(t.respuestas.count(), 2)

        # el equipo responde y cierra; el cliente ya no puede responder
        self.client.force_login(self.admin)
        self.client.post(reverse("admin_ticket", args=[t.pk]), {"mensaje": "Genial", "cerrar": "1"})
        t.refresh_from_db()
        self.assertEqual(t.estado, "cerrado")
        self.client.force_login(self.cli)
        self.client.post(reverse("ver_ticket", args=[t.pk]), {"mensaje": "otra cosa"})
        self.assertEqual(t.respuestas.count(), 3)

    def test_permisos(self):
        self.abrir()
        t = SupportTicket.objects.get()
        self.client.force_login(crear_usuario("otro"))
        self.assertEqual(self.client.get(reverse("ver_ticket", args=[t.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("admin_tickets")).status_code, 302)



class DeclaracionJuradaTests(BaseTest):
    DATOS = {
        "es_pep": "no", "es_sujeto_obligado": "no", "actividad": "Comerciante", "origen_fondos": "comercial",
        "ingresos_mensuales": "1m_5m", "volumen_mensual_previsto": "hasta_1m",
        "residencia_fiscal_exterior": "no", "declara_veracidad": "on",
    }

    def setUp(self):
        super().setUp()
        self.u = Usuario.objects.create_user("nuevo", "n@test.com", "Clave-Segura-123", email_confirmed=True)
        self.client.force_login(self.u)

    def test_sin_declaracion_no_opera(self):
        self.assertRedirects(self.client.get(reverse("operar")), reverse("declaracion_jurada"),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("declaracion_jurada"),
                             fetch_redirect_response=False)

    def test_completa_y_queda_auditada(self):
        resp = self.client.post(reverse("declaracion_jurada"), self.DATOS)
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        d = self.u.declaracion_vigente
        self.assertEqual((d.es_pep, d.origen_fondos, d.version), (False, "comercial", settings.DDJJ_VERSION))
        self.assertTrue(RegistroAuditoria.objects.filter(accion="declaracion_jurada").exists())
        self.assertFalse(AlertaCompliance.objects.exists())
        # con declaración pero sin KYC: pantalla de verificación
        self.assertContains(self.client.get(reverse("operar")), "Estamos verificando tu identidad")

    def test_validaciones(self):
        casos = [
            {**self.DATOS, "declara_veracidad": ""},             # sin confirmar
            {**self.DATOS, "es_pep": "si"},                       # PEP sin cargo
            {**self.DATOS, "origen_fondos": "otro"},              # otro sin detalle
            {**self.DATOS, "residencia_fiscal_exterior": "si"},   # sin país
            {**self.DATOS, "es_pep": ""},                         # sin responder
        ]
        for datos in casos:
            self.assertEqual(self.client.post(reverse("declaracion_jurada"), datos).status_code, 200)
        self.assertIsNone(self.u.declaracion_vigente)

    def test_pep_genera_alerta_y_se_guarda_historial(self):
        self.client.post(reverse("declaracion_jurada"), self.DATOS)
        self.client.post(reverse("declaracion_jurada"), {
            **self.DATOS, "es_pep": "si", "pep_tipo": "familiar", "pep_cargo": "Intendente", "pep_organismo": "Municipio",
        })
        self.assertEqual(self.u.declaraciones.count(), 2)
        self.assertTrue(self.u.declaracion_vigente.es_pep)
        alerta = AlertaCompliance.objects.get(tipo="pep")
        self.assertIn("Intendente", alerta.detalle)

    def test_admin_no_aprueba_kyc_sin_declaracion(self):
        admin = crear_usuario("admin")
        admin.is_staff, admin.rol_staff = True, "superadmin"
        admin.save()
        self.client.force_login(admin)
        self.client.post(reverse("cambiar_estado_verificacion", args=[self.u.pk]), {"estado": "aprobado"})
        self.u.refresh_from_db()
        self.assertEqual(self.u.estado_verificacion, "pendiente")
        crear_declaracion(self.u)
        self.client.post(reverse("cambiar_estado_verificacion", args=[self.u.pk]), {"estado": "aprobado"})
        self.u.refresh_from_db()
        self.assertEqual(self.u.estado_verificacion, "aprobado")



@override_settings(BOLETOS_TOLERAR_ERROR_PDF=True)
class AvisosEmailTests(BaseTest):
    def setUp(self):
        super().setUp()
        from django.core import mail
        self.outbox = mail.outbox
        crear_cotizaciones()
        self.cli = crear_usuario(saldo_ars="500000")
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        cfg = ExchangeConfig.current()
        cfg.avisos_depositos = "caja@finanxis.test"
        cfg.avisos_retiros = "tesoreria@finanxis.test, jefe@finanxis.test"
        cfg.avisos_compliance = "oficial@finanxis.test"
        cfg.save()

    def para(self, asunto_contiene):
        return [m.to for m in self.outbox if asunto_contiene in m.subject]

    def test_retiro_avisa_al_equipo_y_al_cliente(self):
        self.client.force_login(self.cli)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("solicitar_retiro"), {"monto": "1000", "alias": "mi.alias"})
        self.assertEqual(self.para("Nuevo retiro"), [["tesoreria@finanxis.test", "jefe@finanxis.test"]])
        self.assertEqual(self.para("Pedido de retiro recibido"), [[self.cli.email]])
        r = RetiroARS.objects.get()
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("aprobar_retiro", args=[r.pk]))
        self.assertEqual(self.para("Retiro aprobado"), [[self.cli.email]])
        html = [m for m in self.outbox if "Retiro aprobado" in m.subject][0].alternatives[0][0]
        self.assertIn("$ 1.000,00", html)

    def test_deposito_y_operacion_avisan_al_cliente(self):
        dep = DepositoARS.objects.create(usuario=self.cli, monto=Decimal("1500"), comprobante="c.png")
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("aprobar_deposito", args=[dep.pk]))
        self.assertEqual(self.para("Depósito acreditado"), [[self.cli.email]])
        self.client.force_login(self.cli)
        with self.captureOnCommitCallbacks(execute=True):
            operar(self.client, "compra", "USDT", "10200")
        self.assertEqual(self.para("Operación realizada"), [[self.cli.email]])

    def test_sin_destinatarios_no_se_manda_y_alerta_va_a_compliance(self):
        cfg = ExchangeConfig.current()
        cfg.avisos_retiros = ""
        cfg.save()
        self.client.force_login(self.cli)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("solicitar_retiro"), {"monto": "1000", "alias": "mi.alias"})
            AlertaCompliance.objects.create(usuario=self.cli, tipo="umbral", detalle="prueba", monto_ars=Decimal("6000000"))
        self.assertEqual(self.para("Nuevo retiro"), [])
        self.assertEqual(self.para("Alerta de compliance"), [["oficial@finanxis.test"]])

    def test_parametros_validan_emails_y_prueba(self):
        self.client.force_login(self.admin)
        datos = {f: getattr(ExchangeConfig.current(), f) for f in (
            "spread_bps_usdt", "spread_bps_usd", "swap_fee_bps", "umbral_alerta_ars",
            "limite_mensual_operado_ars", "limite_diario_retiro_ars")}
        resp = self.client.post(reverse("guardar_config_exchange"), {**datos, "avisos_retiros": "no-es-un-email"})
        self.assertContains(resp, "Email inválido: no-es-un-email")
        self.client.post(reverse("guardar_config_exchange"), {**datos, "avisos_retiros": "A@x.com; b@x.com\na@x.com"})
        self.assertEqual(ExchangeConfig.current().avisos_retiros, "a@x.com, b@x.com")
        self.client.post(reverse("probar_email"))
        self.assertEqual(self.para("Prueba de email"), [[self.admin.email]])
        self.assertContains(self.client.get(reverse("parametros_exchange")), "Desde dónde salen los emails")



class TitularidadRetiroTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.cli = crear_usuario(saldo_ars="10000")
        self.admin = crear_usuario("admin")
        self.admin.is_staff, self.admin.rol_staff = True, "superadmin"
        self.admin.save()
        self.client.force_login(self.cli)
        self.client.post(reverse("solicitar_retiro"), {"monto": "4000", "alias": "otra.persona"})
        self.r = RetiroARS.objects.get()
        self.client.force_login(self.admin)
        self.client.post(reverse("aprobar_retiro", args=[self.r.pk]))

    def test_no_se_envia_sin_confirmar_titular(self):
        self.client.post(reverse("enviar_retiro", args=[self.r.pk]))
        self.r.refresh_from_db()
        self.assertEqual(self.r.estado, "aprobado")
        with self.settings(BOLETOS_TOLERAR_ERROR_PDF=True):
            self.client.post(reverse("enviar_retiro", args=[self.r.pk]), {"titular": "1"})
        self.r.refresh_from_db()
        self.assertEqual((self.r.estado, self.r.titular_verificado), ("enviado", True))

    def test_rechazo_exige_motivo_y_lo_informa(self):
        from django.core import mail
        self.client.post(reverse("rechazar_retiro_ars", args=[self.r.pk]))
        self.r.refresh_from_db()
        self.assertEqual(self.r.estado, "aprobado")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("rechazar_retiro_ars", args=[self.r.pk]),
                             {"motivo": "La cuenta no está a nombre del cliente"})
        self.r.refresh_from_db()
        self.cli.refresh_from_db()
        self.assertEqual((self.r.estado, self.r.motivo_rechazo), ("rechazado", "La cuenta no está a nombre del cliente"))
        self.assertEqual(self.cli.saldo_ars, Decimal("10000"))
        self.assertIn("La cuenta no está a nombre del cliente", mail.outbox[-1].body)
        self.assertTrue(RegistroAuditoria.objects.filter(accion="retiro_rechazado", detalle__motivo__icontains="nombre").exists())



class RolesTests(BaseTest):
    def staff(self, username, rol):
        u = crear_usuario(username)
        u.is_staff, u.rol_staff = True, rol
        u.save()
        return u

    def setUp(self):
        super().setUp()
        self.cli = crear_usuario()
        self.nuevo = crear_usuario("nuevo", aprobado=False)
        crear_declaracion(self.nuevo)
        self.dep = DepositoARS.objects.create(usuario=self.cli, monto=Decimal("1500"), comprobante="c.png")

    def test_operador(self):
        self.client.force_login(self.staff("op", "operador"))
        cola = self.client.get(reverse("panel_admin")).context["cola"]
        self.assertEqual({c["tipo"] for c in cola}, {"deposito_ars"})
        self.assertEqual(self.client.get(reverse("panel_depositos")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin_cotizaciones")).status_code, 200)
        for nombre in ("parametros_exchange", "exchange_dashboard", "compliance_alertas", "admin_equipo"):
            self.assertEqual(self.client.get(reverse(nombre)).status_code, 403, nombre)
        self.client.post(reverse("cambiar_estado_verificacion", args=[self.nuevo.pk]), {"estado": "aprobado"})
        self.nuevo.refresh_from_db()
        self.assertEqual(self.nuevo.estado_verificacion, "pendiente")

    def test_compliance(self):
        self.client.force_login(self.staff("oc", "compliance"))
        cola = self.client.get(reverse("panel_admin")).context["cola"]
        self.assertEqual({c["tipo"] for c in cola}, {"kyc"})
        self.client.post(reverse("aprobar_deposito", args=[self.dep.pk]))
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.estado, "pendiente")
        self.client.post(reverse("cambiar_estado_verificacion", args=[self.nuevo.pk]), {"estado": "aprobado"})
        self.nuevo.refresh_from_db()
        self.assertEqual(self.nuevo.estado_verificacion, "aprobado")
        self.assertEqual(self.client.get(reverse("auditoria")).status_code, 200)

    def test_contabilidad_y_staff_sin_rol(self):
        self.client.force_login(self.staff("cont", "contabilidad"))
        self.assertEqual(self.client.get(reverse("exchange_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("panel_retiros")).status_code, 403)
        self.client.force_login(self.staff("sinrol", ""))
        self.assertContains(self.client.get(reverse("panel_admin")), "Todavía no tenés un rol", status_code=403)

    def test_equipo_solo_superadmin(self):
        jefe = self.staff("jefe", "superadmin")
        self.client.force_login(jefe)
        self.client.post(reverse("admin_equipo"), {"accion": "rol", "usuario": "cliente", "rol": "operador"})
        self.cli.refresh_from_db()
        self.assertEqual((self.cli.is_staff, self.cli.rol_staff), (True, "operador"))
        self.assertTrue(RegistroAuditoria.objects.filter(accion="equipo_rol").exists())
        self.client.post(reverse("admin_equipo"), {"accion": "quitar", "usuario": "cliente"})
        self.cli.refresh_from_db()
        self.assertEqual((self.cli.is_staff, self.cli.rol_staff), (False, ""))
        # no se cambia el propio rol
        self.client.post(reverse("admin_equipo"), {"accion": "quitar", "usuario": "jefe"})
        jefe.refresh_from_db()
        self.assertTrue(jefe.is_staff)



class LimitesTests(BaseTest):
    def test_registro_se_limita_por_ip(self):
        for _ in range(5):
            self.assertEqual(self.client.post(reverse("registro"), {}).status_code, 200)
        resp = self.client.post(reverse("registro"), {})
        self.assertContains(resp, "Demasiados intentos", status_code=429)
        # mirar el formulario sigue permitido
        self.assertEqual(self.client.get(reverse("registro")).status_code, 200)

    def test_por_usuario_y_json(self):
        u = crear_usuario()
        u.email_confirmed = False
        u.save()
        self.client.force_login(u)
        for _ in range(3):
            self.client.post(reverse("resend_verification"))
        self.assertEqual(self.client.post(reverse("resend_verification")).status_code, 429)
        for _ in range(120):
            self.client.get(reverse("geo_paises"))
        resp = self.client.get(reverse("geo_paises"))
        self.assertEqual((resp.status_code, resp["Content-Type"]), (429, "application/json"))


@override_settings(COTIZACION_ACTUALIZAR_AL_VUELO=True)
class SaludSitioTests(BaseTest):
    def test_pantalla_y_permisos(self):
        crear_cotizaciones()
        jefe = crear_usuario("jefe")
        jefe.is_staff, jefe.rol_staff = True, "superadmin"
        jefe.save()
        self.client.force_login(jefe)
        resp = self.client.get(reverse("salud_sitio"))
        self.assertEqual(resp.status_code, 200)
        titulos = {c["titulo"] for _, cs in resp.context["grupos"] for c in cs}
        self.assertTrue({"Base de datos", "Cache", "Conciliación de saldos", "Envío de emails"} <= titulos)
        op = crear_usuario("op")
        op.is_staff, op.rol_staff = True, "operador"
        op.save()
        self.client.force_login(op)
        self.assertEqual(self.client.get(reverse("salud_sitio")).status_code, 403)

    def test_errores_quedan_registrados(self):
        import logging

        from .models import ErrorRegistrado
        logging.getLogger("usuarios.services.avisos").warning("No se pudo enviar el email de prueba")
        logging.getLogger("django.request").info("esto no se guarda")
        try:
            1 / 0
        except ZeroDivisionError:
            logging.getLogger("django.request").exception("Error interno")
        niveles = list(ErrorRegistrado.objects.order_by("id").values_list("nivel", flat=True))
        self.assertEqual(niveles, ["WARNING", "ERROR"])
        self.assertIn("ZeroDivisionError", ErrorRegistrado.objects.get(nivel="ERROR").detalle)


@override_settings(BOLETOS_TOLERAR_ERROR_PDF=True)
class LocalFisicoTests(BaseTest):
    def setUp(self):
        super().setUp()
        crear_cotizaciones()
        self.cli = crear_usuario()
        self.op = crear_usuario("op")
        self.op.is_staff, self.op.rol_staff = True, "operador"
        self.op.save()
        self.client.force_login(self.op)

    def activar(self, valor=True):
        cfg = ExchangeConfig.current()
        cfg.local_fisico_activo = valor
        cfg.save()

    def test_apagado_por_defecto(self):
        self.assertEqual(self.client.get(reverse("local_fisico")).status_code, 404)
        self.assertNotContains(self.client.get(reverse("panel_admin")), reverse("local_fisico"))

    def test_ingreso_y_retiro_en_efectivo(self):
        from .services.ledger import diferencias_de_conciliacion
        self.activar()
        self.assertContains(self.client.get(reverse("panel_admin")), reverse("local_fisico"))
        self.assertContains(self.client.get(reverse("local_fisico"), {"q": "cliente"}), "cliente")
        url = reverse("local_fisico_cliente", args=[self.cli.pk])
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"tipo": "ingreso", "moneda": "ARS", "monto": "1000"})
            self.client.post(url, {"tipo": "ingreso", "moneda": "USD", "monto": "300", "nota": "Recibo 15"})
            self.client.post(url, {"tipo": "retiro", "moneda": "ARS", "monto": "400"})
        self.cli.refresh_from_db()
        self.assertEqual((self.cli.saldo_usd, self.cli.saldo_ars), (Decimal("300"), Decimal("600")))
        self.assertEqual(set(BoletoOperacion.objects.filter(usuario=self.cli).values_list("tipo", flat=True)),
                         {"local_ingreso", "local_retiro"})
        self.assertEqual(set(ApunteExchange.objects.values_list("categoria", flat=True)) & {"entrada", "salida"},
                         {"entrada", "salida"})
        self.assertEqual(diferencias_de_conciliacion([self.cli]), [])
        self.assertTrue(RegistroAuditoria.objects.filter(accion="local_ingreso").exists())
        # no se puede retirar más que el saldo
        self.client.post(url, {"tipo": "retiro", "moneda": "USD", "monto": "301"})
        self.cli.refresh_from_db()
        self.assertEqual(self.cli.saldo_usd, Decimal("300"))

    def test_permisos_y_cliente_sin_verificar(self):
        self.activar()
        nuevo = crear_usuario("nuevo", aprobado=False)
        self.client.post(reverse("local_fisico_cliente", args=[nuevo.pk]), {"tipo": "ingreso", "moneda": "ARS", "monto": "10"})
        nuevo.refresh_from_db()
        self.assertEqual(nuevo.saldo_ars, Decimal("0"))
        cont = crear_usuario("cont")
        cont.is_staff, cont.rol_staff = True, "contabilidad"
        cont.save()
        self.client.force_login(cont)
        self.assertEqual(self.client.get(reverse("local_fisico")).status_code, 403)
