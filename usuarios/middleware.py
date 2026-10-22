from contextvars import ContextVar

_request_actual = ContextVar('request_actual', default=None)


def request_actual():
    """La request en curso (o None fuera de una request: Celery, comandos, tests)."""
    return _request_actual.get()


class RequestActualMiddleware:
    """Expone la request a services/auditoria.py para registrar la IP de cada acción."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_actual.set(request)
        try:
            return self.get_response(request)
        finally:
            _request_actual.reset(token)
