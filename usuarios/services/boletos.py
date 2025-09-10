# usuarios/services/boletos.py
import io, qrcode, base64, hashlib, uuid
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.timezone import localtime
from django.urls import reverse
from django.core.files.base import ContentFile
from usuarios.models import BoletoOperacion

def _get_weasyprint_HTML():
    from weasyprint import HTML
    return HTML

def _qr_b64(text: str) -> str:
    qr = qrcode.QRCode(box_size=3, border=2)
    qr.add_data(text); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")

def emitir_boleto(usuario, tipo: str, numero: str, snapshot: dict,
                  movimiento=None, onchain: dict | None = None):
    """
    Renderiza boletos/boleto_base.html una sola vez (sin pdf_sha256 impreso).
    Calcula SHA-256 de los bytes finales y lo guarda en BoletoOperacion.pdf_sha256.
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
    try:
        ver_url = settings.SITE_URL.rstrip("/") + reverse("verificar_boleto", args=[numero])
    except Exception:
        ver_url = settings.SITE_URL.rstrip("/") + f"/boletos/{numero}/"

    verif_code = uuid.uuid4().hex[:8].upper()  # corto y legible para imprimir

    # 1) Render ÚNICO del PDF (NO incluimos pdf_sha256 en el contenido)
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
        "verificacion_code": verif_code,
        # IMPORTANTE: NO pasar "pdf_sha256" al template para evitar auto-referencia.
        "qr_base64": _qr_b64(ver_url),
    }
    html = render_to_string("boletos/boleto_base.html", ctx)
    HTML = _get_weasyprint_HTML()
    pdf_bytes = HTML(string=html, base_url=settings.SITE_URL).write_pdf()

    # 2) Hash de los bytes FINALES
    sha_hex = hashlib.sha256(pdf_bytes).hexdigest().upper()

    # 3) Persistimos en los CAMPOS CORRECTOS DEL MODELO
    boleto = BoletoOperacion.objects.create(
        usuario=usuario,
        tipo=tipo,
        numero=numero,
        snapshot=snapshot,         # JSON con lo que imprimiste
        pdf_sha256=sha_hex,        # hash real del archivo final
        verificacion_code=verif_code,
        movimiento=movimiento,
        red=onchain_ctx["red"],
        wallet_origen=onchain_ctx["origen"],
        wallet_destino=onchain_ctx["destino"],
        txid=onchain_ctx["txid"],
    )
    boleto.pdf.save(f"{numero}.pdf", ContentFile(pdf_bytes), save=True)
    return boleto



# import io, qrcode, hashlib, base64, uuid
# from django.template.loader import render_to_string
# from django.conf import settings
# from django.utils.timezone import localtime
# from weasyprint import HTML
# from ..models import BoletoOperacion
# from decimal import Decimal

# def _fmt_money(val, symbol='$'):
#     return f"{symbol}{Decimal(val):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

# def emitir_boleto(usuario, tipo, numero, snapshot, movimiento=None, onchain=None):
#     """
#     snapshot: dict con todos los campos ya calculados (monto_debitado, comisiones, tasa, etc.)
#     onchain: dict opcional: {red, origen, destino, txid, fecha_hora}
#     """
#     verif_code = uuid.uuid4().hex
#     url_verificacion = f"{settings.SITE_URL}/boletos/verify/{verif_code}/"

#     # QR con URL de verificación
#     qr = qrcode.make(url_verificacion)
#     buf = io.BytesIO()
#     qr.save(buf, format="PNG")
#     qr_b64 = base64.b64encode(buf.getvalue()).decode()

#     ctx = {
#         'titulo': snapshot.get('titulo', 'Comprobante de operación'),
#         'numero': numero,
#         'fecha_emision': localtime().strftime('%d/%m/%Y %H:%M:%S'),
#         'estado': snapshot.get('estado', 'Completo'),
#         'monto_debitado_fmt': snapshot.get('monto_debitado_fmt'),
#         'comision_total_fmt': snapshot.get('comision_total_fmt'),
#         'monto_origen_fmt': snapshot.get('monto_origen_fmt'),
#         'tasa_fmt': snapshot.get('tasa_fmt'),
#         'monto_destino_fmt': snapshot.get('monto_destino_fmt'),
#         'cliente': snapshot['cliente'],
#         'psav': snapshot['psav'],
#         'onchain': {
#             'muestra': bool(onchain and (onchain.get('txid') or onchain.get('red'))),
#             'red': onchain.get('red','') if onchain else '',
#             'origen': onchain.get('origen','') if onchain else '',
#             'destino': onchain.get('destino','') if onchain else '',
#             'txid': onchain.get('txid','') if onchain else '',
#             'fecha_hora': onchain.get('fecha_hora','') if onchain else '',
#         },
#         'verificacion_code': verif_code,
#         'url_verificacion': url_verificacion,
#         'qr_base64': qr_b64,
#         'pdf_sha256': '',  # lo llenamos luego
#     }

#     html = render_to_string('boletos/boleto_base.html', ctx)
#     pdf_bytes = HTML(string=html, base_url=settings.SITE_URL).write_pdf()

#     # Hash del PDF
#     sha = hashlib.sha256(pdf_bytes).hexdigest()
#     ctx['pdf_sha256'] = sha  # dejalo impreso en pantalla también (opcional)

#     # Re-render con el hash si querés que salga en el PDF
#     html = render_to_string('boletos/boleto_base.html', ctx)
#     pdf_bytes = HTML(string=html, base_url=settings.SITE_URL).write_pdf()
#     sha = hashlib.sha256(pdf_bytes).hexdigest()

#     # Guardar archivo
#     fname = f"boletos/{numero}.pdf"
#     from django.core.files.base import ContentFile
#     content = ContentFile(pdf_bytes)
#     boleto = BoletoOperacion.objects.create(
#         usuario=usuario,
#         movimiento=movimiento,
#         tipo=tipo,
#         numero=numero,
#         snapshot=snapshot,
#         pdf_sha256=sha,
#         verificacion_code=verif_code,
#         red=(onchain or {}).get('red',''),
#         wallet_origen=(onchain or {}).get('origen',''),
#         wallet_destino=(onchain or {}).get('destino',''),
#         txid=(onchain or {}).get('txid',''),
#     )
#     boleto.pdf.save(f"{numero}.pdf", content, save=True)
#     return boleto
