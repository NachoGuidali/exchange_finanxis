from django.conf import settings


def demo(request):
    return {"DEMO_MODE": getattr(settings, "DEMO_MODE", False)}


def pendientes_admin(request):
    """Contadores de la barra lateral del panel admin (solo staff y solo en /admin-dashboard/)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_staff and request.path.startswith("/admin-dashboard")):
        return {}
    from .models import AlertaCompliance, DepositoARS, DepositoUSDT, RetiroARS, RetiroCrypto, SupportTicket, Usuario

    from .permisos import permisos_de, rol_de

    puede = permisos_de(user)
    p = {"kyc": 0, "depositos_ars": 0, "depositos_usdt": 0, "retiros": 0, "alertas": 0}
    # cada rol cuenta solo lo que puede resolver
    if "identidad" in puede:
        p["kyc"] = Usuario.objects.filter(estado_verificacion="pendiente").count()
    if "fondos" in puede:
        p["depositos_ars"] = DepositoARS.objects.filter(estado="pendiente").count()
        p["depositos_usdt"] = DepositoUSDT.objects.filter(estado="pendiente").count()
        p["retiros"] = (RetiroARS.objects.filter(estado__in=("pendiente", "aprobado")).count()
                        + RetiroCrypto.objects.filter(estado="pendiente").count())
    if "compliance" in puede:
        p["alertas"] = AlertaCompliance.objects.filter(estado="abierta").count()
    p["total"] = sum(p.values())
    # los tickets se cuentan aparte: no son decisiones sobre dinero o identidad
    p["tickets"] = SupportTicket.objects.filter(estado="abierto").count()

    # aviso en la barra lateral: precio manual activo o cotización vencida
    from .services.cotizaciones import FUENTES, ultima
    from .utils import cotizacion_vigente
    ultimas = [ultima(m) for m in FUENTES]
    if any(c and c.manual_activa for c in ultimas):
        estado_cot = "manual"
    elif not all(cotizacion_vigente(c) for c in ultimas):
        estado_cot = "vencida"
    else:
        estado_cot = ""
    return {"pendientes": p, "estado_cotizacion": estado_cot, "puede": puede, "rol_staff": rol_de(user)}
