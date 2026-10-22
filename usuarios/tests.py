import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
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


@override_settings(MEDIA_ROOT=MEDIA_TMP)
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
        self.assertEqual(self.client.get(self.url).status_code, 302)

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
        resp = self.client.post(self.url, {"operacion": "compra", "moneda": "USDT", "monto": "10200"})
        self.assertEqual(resp.status_code, 302)
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
        resp = self.client.post(self.url, {"operacion": "compra", "moneda": "USDT", "monto": "1000"})
        self.assertEqual(resp.status_code, 400)
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
