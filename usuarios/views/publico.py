"""Páginas públicas: home, FAQ, términos y verificación de boletos."""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import BoletoOperacion


def home(request):
    return render(request, "home.html")


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
