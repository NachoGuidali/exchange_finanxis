"""
Roles del equipo y qué puede hacer cada uno en el panel de administración.

Todo el staff con rol ve Pendientes (solo lo que le toca), Usuarios, Movimientos y Soporte.
El superadmin (o un superusuario de Django) puede todo. Staff sin rol no entra al panel.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

ROLES = [
    ('superadmin', 'Superadmin'),
    ('operador', 'Operador'),
    ('compliance', 'Compliance'),
    ('contabilidad', 'Contabilidad'),
]

# permiso → roles que lo tienen (además del superadmin)
PERMISOS = {
    'panel': {'operador', 'compliance', 'contabilidad'},   # pendientes, usuarios, movimientos, soporte
    'fondos': {'operador'},                                 # depósitos y retiros
    'cotizaciones': {'operador'},
    'local': {'operador'},                                  # efectivo en el local físico (si está activo)
    'identidad': {'compliance'},                            # aprobar o rechazar KYC
    'compliance': {'compliance'},                           # alertas
    'auditoria': {'compliance', 'contabilidad'},
    'contabilidad': {'contabilidad'},
    'parametros': set(),                                    # solo superadmin
    'equipo': set(),                                        # solo superadmin
    'salud': set(),                                         # solo superadmin
}

# qué parte de la cola de pendientes ve cada permiso
PENDIENTES_POR_PERMISO = {
    'identidad': {'kyc'},
    'fondos': {'deposito_ars', 'deposito_usdt', 'retiro_ars', 'retiro_crypto'},
    'compliance': {'alerta'},
}


def rol_de(user):
    if not getattr(user, 'is_authenticated', False):
        return ''
    if user.is_superuser:
        return 'superadmin'
    return user.rol_staff if user.is_staff else ''


def puede(user, permiso):
    rol = rol_de(user)
    return rol == 'superadmin' or (bool(rol) and rol in PERMISOS[permiso])


def permisos_de(user):
    return {p for p in PERMISOS if puede(user, p)}


def pendientes_visibles(user):
    return set().union(*(tipos for p, tipos in PENDIENTES_POR_PERMISO.items() if puede(user, p)))


def requiere(permiso):
    """Vista del panel: exige sesión, ser staff con rol y tener el permiso."""
    def decorador(view):
        @login_required
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_staff:
                return redirect(f"/login/?next={request.path}")
            if not rol_de(request.user):
                return render(request, 'usuarios/sin_permiso.html', {'sin_rol': True}, status=403)
            if not puede(request.user, permiso):
                return render(request, 'usuarios/sin_permiso.html', status=403)
            return view(request, *args, **kwargs)
        return _wrapped
    return decorador
