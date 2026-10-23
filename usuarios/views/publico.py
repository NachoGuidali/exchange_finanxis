"""Páginas públicas: home, FAQ, términos y verificación de boletos."""

from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import BoletoOperacion, Cotizacion, ExchangeConfig
from ..services import cotizaciones
from ..utils import cotizacion_vigente


def _ultimas_cotizaciones():
    def ultimas():
        return (Cotizacion.objects.filter(moneda="USDT").order_by("-fecha").first(),
                Cotizacion.objects.filter(moneda="USD").order_by("-fecha").first())
    usdt, usd = ultimas()
    vigente = cotizacion_vigente(usdt) and cotizacion_vigente(usd)
    if settings.COTIZACION_ACTUALIZAR_AL_VUELO and cotizaciones.actualizar_si_vencida(vigente):
        usdt, usd = ultimas()
        vigente = cotizacion_vigente(usdt) and cotizacion_vigente(usd)
    return usdt, usd, vigente


def home(request):
    cot_usdt, cot_usd, vigente = _ultimas_cotizaciones()
    ejemplo = (Decimal(200000) / cot_usdt.venta) if cot_usdt and cot_usdt.venta else None
    return render(request, "home.html", {
        "ejemplo_usdt": ejemplo,
        "cot_usdt": cot_usdt, "cot_usd": cot_usd, "cotizacion_vigente": vigente,
        "swap_fee_pct": f"{ExchangeConfig.current().swap_fee_bps / 100:g}".replace(".", ","),
    })


def faq(request):
    # Podés mover esto a BD si querés
    faqs = [
        {"q":"¿Cómo compro USDT con ARS?",
         "a":"Ir a Operar → Comprar, elegí USDT, ingresá el monto en ARS y confirmá el resumen."},
        {"q":"¿Cuál es la comisión del swap USD ⇄ USDT?",
         "a":"Mostramos la tasa base y los bps de comisión en el formulario. El preview ya calcula el monto neto."},
        {"q":"¿Dónde veo mis movimientos?",
         "a":"En Dashboard aparecen los últimos 10. Para ver y exportar: Movimientos (en el menú)."},
        {"q":"¿Cómo verifico mi identidad (KYC)?",
         "a":"Durante el registro, en el paso KYC, subí frente y dorso de tu DNI con buena iluminación."},
        {"q":"¿Puedo retirar USD en sucursal?",
         "a":"Sí. Comprá USD en la app y acercate a una sucursal habilitada para retiro en efectivo."},
    ]
    return render(request, "usuarios/faq.html", {"faqs": faqs})


def tyc(request):
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect('login')
        request.user.marcar_tyc_aceptado(getattr(settings,"TYC_VERSION",""))
        messages.success(request, "Términos aceptados.")
        next_url = request.POST.get("next") or ""
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
            next_url = "dashboard"
        return redirect(next_url)
    return render(request, "usuarios/tyc.html", {"version": getattr(settings,"TYC_VERSION","")})


def verificar_boleto(request, numero):
    b = get_object_or_404(BoletoOperacion, numero=numero)
    ctx = {
        "numero": b.numero,
        "fecha": b.fecha_emision,
        "usuario": b.usuario.username,
        "tipo": b.get_tipo_display(),
        "pdf_sha256": b.pdf_sha256,
        "anulado": b.anulado,
    }
    return render(request, "boletos/verificar.html", ctx)
