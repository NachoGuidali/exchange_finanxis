import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import DepositoARSForm
from .models import (
    ApunteExchange, BoletoOperacion, Cotizacion, CuentaExchange, DepositoARS,
    Movimiento, RetiroARS, RetiroCrypto, Usuario,
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
    return u


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
        self.client.post(self.url, {"moneda": "USDT", "monto": "-1000000", "direccion_wallet": "T1"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_usdt, Decimal("10"))
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_moneda_ars_rechazada(self):
        u = crear_usuario(saldo_ars="500000")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "ARS", "monto": "500000", "direccion_wallet": "T1"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_ars, Decimal("500000"))
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_saldo_insuficiente(self):
        u = crear_usuario(saldo_usdt="5")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "6", "direccion_wallet": "T1"})
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_retiro_ok_descuenta_y_registra_negativo(self):
        u = crear_usuario(saldo_usdt="100")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "40", "direccion_wallet": "T1"})
        u.refresh_from_db()
        self.assertEqual(u.saldo_usdt, Decimal("60"))
        mov = Movimiento.objects.get(usuario=u, tipo="retiro")
        self.assertEqual(mov.monto, Decimal("-40"))

    def test_requiere_kyc(self):
        u = crear_usuario(aprobado=False, saldo_usdt="100")
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USDT", "monto": "40", "direccion_wallet": "T1"})
        self.assertFalse(RetiroCrypto.objects.exists())

    def test_get_no_rompe(self):
        self.client.force_login(crear_usuario())
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_rechazo_recredita_en_la_misma_moneda(self):
        u = crear_usuario(saldo_usd="100")
        admin = crear_usuario("admin")
        admin.is_staff = True
        admin.save()
        self.client.force_login(u)
        self.client.post(self.url, {"moneda": "USD", "monto": "30", "direccion_wallet": "T1"})
        r = RetiroCrypto.objects.get()
        self.client.force_login(admin)
        self.client.post(reverse("rechazar_retiro_cripto", args=[r.id]))
        u.refresh_from_db()
        self.assertEqual((u.saldo_usd, u.saldo_usdt), (Decimal("100"), Decimal("0")))


class RetiroARSTests(BaseTest):
    url = reverse("solicitar_retiro")

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
        admin.is_staff = True
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
        admin.is_staff = True
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
        self.admin.is_staff = True
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

        r1 = fondos.solicitar_retiro_ars(u, Decimal("1000"), alias="a")
        fondos.aprobar_retiro_ars(r1.pk)
        fondos.enviar_retiro_ars(r1.pk, self.admin)
        r2 = fondos.solicitar_retiro_crypto(u, "USDT", Decimal("1"), "T1")
        fondos.rechazar_retiro_crypto(r2.pk, self.admin)
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
        r = fondos.solicitar_retiro_ars(u, Decimal("300"), alias="a")
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
                fondos.solicitar_retiro_ars(u, Decimal("80"), alias="a")
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
        self.admin.is_staff = True
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
        self.client.post(reverse("solicitar_retiro"), {"monto": "150000", "alias": "a"})
        self.client.post(reverse("solicitar_retiro"), {"monto": "100000", "alias": "a"})  # 250k > 200k
        self.assertEqual(RetiroARS.objects.count(), 1)
        self.assertEqual(self.alertas("limite_excedido").count(), 1)

    def test_retiro_rapido_tras_deposito(self):
        from .services import fondos
        u = crear_usuario()
        dep = DepositoARS.objects.create(usuario=u, monto=Decimal("50000"), comprobante="c.png")
        fondos.aprobar_deposito_ars(dep.pk, self.admin)
        fondos.solicitar_retiro_ars(u, Decimal("49000"), alias="a")
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
        self.staff.is_staff = True
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
        admin.is_staff = True
        admin.save()
        objetivo = crear_usuario("nuevo", aprobado=False)
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

    def test_cargar_demo_solo_en_modo_demo(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("cargar_demo")

    @override_settings(DEMO_MODE=True)
    def test_cartel_demo(self):
        self.assertContains(self.client.get(reverse("login")), "Entorno de demostración")
