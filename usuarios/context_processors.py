from django.conf import settings


def demo(request):
    return {"DEMO_MODE": getattr(settings, "DEMO_MODE", False)}


def pendientes_admin(request):
    """Contadores de la barra lateral del panel admin (solo staff y solo en /admin-dashboard/)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_staff and request.path.startswith("/admin-dashboard")):
        return {}
    from .models import AlertaCompliance, DepositoARS, DepositoUSDT, RetiroARS, RetiroCrypto, Usuario

    p = {
        "kyc": Usuario.objects.filter(estado_verificacion="pendiente").count(),
        "depositos_ars": DepositoARS.objects.filter(estado="pendiente").count(),
        "depositos_usdt": DepositoUSDT.objects.filter(estado="pendiente").count(),
        "retiros": (RetiroARS.objects.filter(estado__in=("pendiente", "aprobado")).count()
                    + RetiroCrypto.objects.filter(estado="pendiente").count()),
        "alertas": AlertaCompliance.objects.filter(estado="abierta").count(),
    }
    p["total"] = sum(p.values())
    return {"pendientes": p}
