"""
Límite de pedidos por IP o por usuario (contra abuso: registros masivos, spam de emails,
consultas automatizadas). Usa la cache (Redis en producción). El login tiene su propio
bloqueo con django-axes.
"""
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

from usuarios.services.auditoria import _ip


def limitar(nombre, maximo, ventana_seg, *, por='ip', metodos=('POST',), json=False):
    """Permite `maximo` pedidos cada `ventana_seg` segundos por IP o por usuario; después responde 429."""
    def decorador(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            if request.method in metodos and getattr(settings, 'LIMITES_ACTIVOS', True):
                quien = request.user.pk if por == 'usuario' and request.user.is_authenticated else _ip(request)
                clave = f"limite:{nombre}:{quien}"
                cache.add(clave, 0, ventana_seg)
                try:
                    usados = cache.incr(clave)
                except ValueError:  # venció entre add e incr
                    cache.set(clave, 1, ventana_seg)
                    usados = 1
                if usados > maximo:
                    minutos = max(1, ventana_seg // 60)
                    if json:
                        return JsonResponse({'error': 'Demasiados pedidos. Probá en un rato.'}, status=429)
                    return render(request, 'usuarios/demasiados_intentos.html', {'minutos': minutos}, status=429)
            return view(request, *args, **kwargs)
        return _wrapped
    return decorador
