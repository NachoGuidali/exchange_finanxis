import base64
import hashlib
import io
import logging
import uuid

import qrcode
from django.conf import settings
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.timezone import localtime

from usuarios.models import BoletoOperacion

logger = logging.getLogger(__name__)


def _get_weasyprint_HTML():
    from weasyprint import HTML
    return HTML

def qr_base64(text: str) -> str:
    qr = qrcode.QRCode(box_size=3, border=2)
    qr.add_data(text); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")

def emitir_boleto(usuario, tipo: str, numero: str, snapshot: dict, movimiento=None, onchain: dict | None = None):
    """
    Genera y PERSISTE la boleta según tu modelo BoletoOperacion.
    Lanza excepción si algo falla (para que la operación sea atómica).
    """
    onchain = onchain or {}
    onchain_ctx = {
        "muestra": any(onchain.get(k) for k in ("red", "origen", "destino", "txid", "fecha_hora")),
        "red": onchain.get("red", ""),
        "origen": onchain.get("origen", ""),
        "destino": onchain.get("destino", ""),
        "txid": onchain.get("txid", ""),
        "fecha_hora": onchain.get("fecha_hora", ""),
    }

    # URL pública de verificación
    base_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        ver_url = base_url + reverse("verificar_boleto", args=[numero])
    except Exception:
        ver_url = f"{base_url}/boletos/{numero}/"

    # 1) context para el template
    ctx = {
        "titulo": snapshot.get("titulo", "Comprobante"),
        "numero": numero,
        "fecha_emision": localtime().strftime("%d/%m/%Y %H:%M:%S"),
        "estado": snapshot.get("estado", "Completo"),
        "monto_debitado_fmt": snapshot.get("monto_debitado_fmt"),
        "comision_total_fmt": snapshot.get("comision_total_fmt"),
        "monto_origen_fmt": snapshot.get("monto_origen_fmt"),
        "tasa_fmt": snapshot.get("tasa_fmt"),
        "monto_destino_fmt": snapshot.get("monto_destino_fmt"),
        "cliente": snapshot.get("cliente", {}),
        "psav": snapshot.get("psav", {}),
        "onchain": onchain_ctx,
        "url_verificacion": ver_url,
        "verificacion_code": f"{numero[:4]}-{numero[-4:]}",
        "qr_base64": qr_base64(ver_url),
        "pdf_sha256": "",  # lo seteo luego
    }

    # 2) HTML → PDF (pre) para hash
    try:
        html_pre = render_to_string("boletos/boleto_base.html", ctx)
        HTML = _get_weasyprint_HTML()
        pdf_bytes = HTML(string=html_pre, base_url=base_url).write_pdf()
        sha_hex = hashlib.sha256(pdf_bytes).hexdigest().upper()

        # 3) (opcional) re-render con hash visible en el PDF
        ctx["pdf_sha256"] = sha_hex
        html_final = render_to_string("boletos/boleto_base.html", ctx)
        pdf_bytes = HTML(string=html_final, base_url=base_url).write_pdf()
    except (ImportError, OSError) as e:
        # WeasyPrint sin librerías de sistema (Pango). En producción esto debe fallar la operación;
        # en la demo el comprobante se guarda igual (se ve en pantalla y se descarga como imagen).
        if not getattr(settings, "BOLETOS_TOLERAR_ERROR_PDF", False):
            raise
        logger.warning("Boleto %s sin PDF (WeasyPrint no disponible): %s", numero, e)
        pdf_bytes, sha_hex = None, ""

    # 4) Persistencia acorde a tu modelo
    boleto = BoletoOperacion.objects.create(
        usuario=usuario,
        movimiento=movimiento,
        tipo=tipo,
        numero=numero,
        snapshot=snapshot,             # guardás el snapshot “de negocio”
        pdf_sha256=sha_hex,
        verificacion_code=uuid.uuid4().hex[:12].upper(),
        red=onchain_ctx["red"],
        wallet_origen=onchain_ctx["origen"],
        wallet_destino=onchain_ctx["destino"],
        txid=onchain_ctx["txid"],
    )
    if pdf_bytes:
        boleto.pdf.save(f"{numero}.pdf", ContentFile(pdf_bytes), save=True)
    return boleto
