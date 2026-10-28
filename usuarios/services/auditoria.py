from django.conf import settings

from usuarios.middleware import request_actual
from usuarios.models import RegistroAuditoria


def _ip(request):
    if request is None:
        return None
    # Detrás de un proxy confiable la IP real viene en X-Forwarded-For (primer valor)
    if getattr(settings, 'AXES_IPWARE_PROXY_COUNT', 0):
        reenviada = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if reenviada:
            return reenviada.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or None


def auditar(actor, accion, objeto='', **detalle):
    """
    Registra una acción sensible. `objeto` puede ser un modelo (se guarda "Modelo:pk") o un texto.
    Los valores de `detalle` deben ser serializables a JSON (se convierten a str si no).
    """
    if hasattr(objeto, 'pk'):
        objeto = f"{type(objeto).__name__}:{objeto.pk}"
    actor = actor if getattr(actor, 'is_authenticated', False) else None
    return RegistroAuditoria.objects.create(
        actor=actor,
        accion=accion,
        objeto=str(objeto)[:128],
        detalle={k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in detalle.items()},
        ip=_ip(request_actual()),
    )


def ip_actual():
    """IP real del pedido en curso (o None fuera de un request)."""
    return _ip(request_actual())
