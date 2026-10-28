"""
Handler de logging que guarda en la base los errores del sitio (y los avisos de la propia app,
como un email que no salió) para mostrarlos en Administración → Salud del sitio.
Nunca rompe el pedido en curso: si no puede guardar, lo descarta.
"""
import logging
import threading
import traceback

_guardando = threading.local()


class GuardarErrores(logging.Handler):
    def emit(self, record):
        # errores de cualquier parte; avisos solo de nuestro código
        if record.levelno < logging.ERROR and not record.name.startswith('usuarios'):
            return
        if getattr(_guardando, 'activo', False):  # un error al guardar no vuelve a entrar acá
            return
        _guardando.activo = True
        try:
            from django.db import connection

            from usuarios.models import ErrorRegistrado
            if connection.in_atomic_block and connection.needs_rollback:
                return  # la transacción ya falló: no hay dónde escribir
            request = getattr(record, 'request', None)
            detalle = ''.join(traceback.format_exception(*record.exc_info)) if record.exc_info else ''
            usuario = getattr(request, 'user', None)
            ErrorRegistrado.objects.create(
                nivel=record.levelname, origen=record.name[:100], mensaje=record.getMessage()[:500],
                ruta=(getattr(request, 'path', '') or '')[:255],
                usuario_id=usuario.pk if getattr(usuario, 'is_authenticated', False) else None,
                detalle=detalle[-20000:],
            )
        except Exception:
            pass
        finally:
            _guardando.activo = False
