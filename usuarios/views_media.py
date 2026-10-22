import mimetypes

from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404

from .models import BoletoOperacion, DepositoARS, DepositoUSDT, SupportTicket, Usuario

# (modelo, campo de archivo, cómo obtener el id del dueño)
ARCHIVOS_PRIVADOS = (
    (Usuario, 'dni_frente', 'id'),
    (Usuario, 'dni_dorso', 'id'),
    (DepositoARS, 'comprobante', 'usuario_id'),
    (DepositoUSDT, 'comprobante', 'usuario_id'),
    (SupportTicket, 'adjunto', 'usuario_id'),
    (BoletoOperacion, 'pdf', 'usuario_id'),
)


def _duenio_de(path):
    """Id del usuario dueño del archivo, o None si no pertenece a ningún registro."""
    for modelo, campo, campo_duenio in ARCHIVOS_PRIVADOS:
        fila = modelo.objects.filter(**{campo: path}).values(campo_duenio).first()
        if fila:
            return fila[campo_duenio]
    return None


@login_required
def media_protegida(request, path):
    """
    Sirve archivos de MEDIA_ROOT solo a su dueño o al staff.
    En producción el servidor web NO debe exponer /media/ directamente.
    """
    duenio = _duenio_de(path)
    if duenio is None or not (request.user.is_staff or request.user.id == duenio):
        raise Http404  # mismo error si no existe o no es tuyo: no revela qué archivos hay

    try:
        archivo = default_storage.open(path, 'rb')
    except (FileNotFoundError, SuspiciousFileOperation):
        raise Http404

    content_type = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    resp = FileResponse(archivo, content_type=content_type)
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    # Un adjunto subido por un usuario nunca debe poder ejecutar scripts en nuestro dominio
    resp['Content-Security-Policy'] = 'sandbox'
    return resp
