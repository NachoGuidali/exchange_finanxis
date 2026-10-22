"""Panel de administración: KYC, aprobación de depósitos/retiros y contabilidad."""
import csv
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.timezone import localtime
from django.views.decorators.http import require_POST

from usuarios.services import fondos, onchain
from usuarios.services.auditoria import auditar
from usuarios.services.comun import OperacionError

from ..forms import ExchangeConfigForm
from ..models import (
    AlertaCompliance,
    ApunteExchange,
    BoletoOperacion,
    CuentaExchange,
    DepositoARS,
    DepositoUSDT,
    ExchangeConfig,
    Movimiento,
    RetiroARS,
    RegistroAuditoria,
    RetiroCrypto,
    Usuario,
)


def es_admin(user):
    return user.is_superuser or user.is_staff


def staff_required(u):
    return u.is_staff or u.is_superuser


@login_required
@user_passes_test(es_admin)
def panel_admin(request):
    q = (request.GET.get('q') or '').strip()

    # Base querysets (sin limitar resultados)
    usuarios_qs   = Usuario.objects.all()
    depositos_qs  = DepositoARS.objects.all()
    retiros_qs    = RetiroARS.objects.all()
    rcrypto_qs    = RetiroCrypto.objects.all()
    movs_qs       = Movimiento.objects.select_related('usuario').order_by('-fecha')[:200]

    # Si querés que búsqueda también afecte a los KPI y tablas:
    if q:
        user_filter = (Q(username__icontains=q) |
                       Q(email__icontains=q)    |
                       Q(doc_nro__icontains=q))
        usuarios_qs  = usuarios_qs.filter(user_filter)
        depositos_qs = depositos_qs.filter(Q(usuario__username__icontains=q) | Q(usuario__email__icontains=q))
        retiros_qs   = retiros_qs.filter(Q(usuario__username__icontains=q) | Q(usuario__email__icontains=q))
        rcrypto_qs   = rcrypto_qs.filter(Q(usuario__username__icontains=q) | Q(usuario__email__icontains=q))
        # movs_qs ya viene limitado a 200; si querés filtrar por q:
        # movs_qs = Movimiento.objects.filter(Q(usuario__username__icontains=q) | ...).order_by('-fecha')[:200]

    # ---------- KPIs (idénticos al criterio de la tabla) ----------
    kpi_pendientes_kyc      = usuarios_qs.filter(estado_verificacion__iexact='pendiente').count()
    kpi_depositos_pend      = depositos_qs.filter(estado__iexact='pendiente').count()
    kpi_retiros_ars_pend    = retiros_qs.filter(estado__in=['pendiente','aprobado']).count()  # si “aprobado” aún no enviado
    kpi_retiros_crypto_pend = rcrypto_qs.filter(estado__iexact='pendiente').count()

    # Listas para las pestañas (podés dejarlas completas y filtrar en el template como ya hacés)
    context = {
        'usuarios': usuarios_qs,
        'depositos': depositos_qs,
        'retiros': retiros_qs,
        'retiros_crypto': rcrypto_qs,
        'movimientos': movs_qs,

        'kpi_pendientes_kyc': kpi_pendientes_kyc,
        'kpi_depositos_pend': kpi_depositos_pend,
        'kpi_retiros_ars_pend': kpi_retiros_ars_pend,
        'kpi_retiros_crypto_pend': kpi_retiros_crypto_pend,
    }
    cfg = ExchangeConfig.current()
    cfg_form = ExchangeConfigForm(instance=cfg)
    context.update({"cfg_form": cfg_form, "cfg": cfg,
                    "kpi_alertas_abiertas": AlertaCompliance.objects.filter(estado='abierta').count()})
    return render(request, 'usuarios/panel_admin.html', context)


@login_required
@user_passes_test(es_admin)
def cambiar_estado_verificacion(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)

    if request.method == 'POST':
        nuevo_estado = request.POST.get('estado')
        if nuevo_estado in ['pendiente', 'aprobado', 'rechazado']:
            anterior = usuario.estado_verificacion
            usuario.estado_verificacion = nuevo_estado
            if nuevo_estado == 'aprobado':
                usuario.is_active = True
            usuario.save()
            auditar(request.user, 'kyc_cambio_estado', usuario, anterior=anterior, nuevo=nuevo_estado)
    return redirect('panel_admin')


@login_required
@user_passes_test(es_admin)
def panel_depositos(request):
    depositos = DepositoARS.objects.all().order_by('-fecha')
    return render(request, 'usuarios/panel_depositos.html', {'depositos':depositos})


@login_required
@user_passes_test(es_admin)
def historial_usuario(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)
    movimientos = Movimiento.objects.filter(usuario=usuario).order_by('-fecha')
    return render(request, 'usuarios/historial_usuario.html', {
        'usuario': usuario,
        'movimientos': movimientos
    })


@login_required
@user_passes_test(es_admin)
def panel_retiros(request):
    retiros_ars = RetiroARS.objects.filter(estado='pendiente').order_by('-fecha_solicitud')
    retiros_crypto = RetiroCrypto.objects.filter(estado='pendiente').order_by('-fecha_solicitud')



    return render(request, 'usuarios/panel_retiros.html', {
        'retiros_ars': retiros_ars,
        'retiros_crypto': retiros_crypto,
    })


@login_required
@user_passes_test(es_admin)
def panel_depositos_usdt(request):
    depositos = DepositoUSDT.objects.all().order_by('-fecha')
    return render(request, 'usuarios/panel_depositos_usdt.html', {'depositos': depositos})


@login_required
@user_passes_test(es_admin)
def exportar_movimientos_admin(request):
    movimientos = Movimiento.objects.all()

    # Filtros
    fecha_desde = request.GET.get('desde')
    fecha_hasta = request.GET.get('hasta')
    moneda = request.GET.get('moneda')
    tipo = request.GET.get('tipo')

    if fecha_desde:
        movimientos = movimientos.filter(fecha__gte=fecha_desde)
    if fecha_hasta:
        movimientos = movimientos.filter(fecha__lte=fecha_hasta)
    if moneda:
        movimientos = movimientos.filter(moneda=moneda)
    if tipo:
        movimientos = movimientos.filter(tipo=tipo)

    movimientos = movimientos.order_by('-fecha')

    # CSV
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="movimientos_todos.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID','Usuario', 'Fecha', 'Tipo', 'Moneda', 'Monto', 'Saldo antes', 'Saldo después', 'Descripción'])

    for m in movimientos:
        writer.writerow([
            m.codigo,
            m.usuario.username,
            localtime(m.fecha).strftime('%Y-%m-%d %H:%M'),
            m.tipo,
            m.moneda,
            m.monto,
            m.saldo_antes,
            m.saldo_despues,
            m.descripcion
        ])

    return response


@login_required
@user_passes_test(es_admin)
def exportar_historial_usuario(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)
    movimientos = Movimiento.objects.filter(usuario=usuario).order_by('-fecha')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="movimientos_{usuario.username}.csv"'

    writer = csv.writer(response)
    writer.writerow(['Fecha', 'Tipo', 'Moneda', 'Monto', 'Saldo antes', 'Saldo después', 'Descripción'])

    for m in movimientos:
        writer.writerow([
            localtime(m.fecha).strftime('%Y-%m-%d %H:%M'),
            m.tipo,
            m.moneda,
            m.monto,
            m.saldo_antes,
            m.saldo_despues,
            m.descripcion
        ])

    return response


@login_required
@user_passes_test(es_admin)
def admin_usuario_perfil(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)
    movimientos = Movimiento.objects.filter(usuario=usuario).order_by("-fecha")[:200]
    retiros = RetiroARS.objects.filter(usuario=usuario).order_by("-fecha_solicitud")
    retiros_crypto = RetiroCrypto.objects.filter(usuario=usuario).order_by("-fecha_solicitud")
    depositos = DepositoARS.objects.filter(usuario=usuario).order_by("-fecha")
    depositos_crypto = DepositoUSDT.objects.filter(usuario=usuario).order_by("-fecha")
    boletos = BoletoOperacion.objects.filter(usuario=usuario).order_by('-fecha_emision')
    return render(request, "usuarios/usuario_perfil.html", {
        "usuario": usuario,
        "movimientos": movimientos,
        "retiros": retiros,
        "retiros_crypto": retiros_crypto,
        "depositos": depositos,
        "depositos_crypto": depositos_crypto,
        "boletos": boletos,
    })


@login_required
@user_passes_test(es_admin)
def admin_usuarios_list(request):
    qs = Usuario.objects.all().order_by('-date_joined')

    # --- filtros ---
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q) |
            Q(email__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(doc_nro__icontains=q)
        )

    estado = request.GET.get('estado', '')
    if estado in ('pendiente', 'aprobado', 'rechazado'):
        qs = qs.filter(estado_verificacion=estado)

    activo = request.GET.get('activo', '')
    if activo in ('1', '0'):
        qs = qs.filter(is_active=(activo == '1'))

    # --- paginación ---
    paginator = Paginator(qs, 50)
    page = request.GET.get('page')
    usuarios = paginator.get_page(page)

    return render(request, "usuarios/admin_usuarios_list.html", {
        "usuarios": usuarios,
        "f": {"q": q, "estado": estado, "activo": activo},
    })


AFFECTS_CASH = {"spread_compra", "spread_venta", "fee_swap", "ajuste"}


def _parse_iso_date(s):
    if not s:
        return None
    try:
        # admite YYYY-MM-DD
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _qs_exchange_from_request(request):
    """
    Centraliza la construcción del queryset con filtros por GET.
    """
    params = {
        "desde": request.GET.get("desde") or "",
        "hasta": request.GET.get("hasta") or "",
        "cat":   request.GET.get("cat")   or "",
        "mon":   request.GET.get("mon")   or "",
    }

    qs = ApunteExchange.objects.select_related("usuario").all()

    d1 = _parse_iso_date(params["desde"])
    d2 = _parse_iso_date(params["hasta"])

    if d1:
        qs = qs.filter(fecha__date__gte=d1)
    if d2:
        qs = qs.filter(fecha__date__lte=d2)

    if params["cat"]:
        qs = qs.filter(categoria=params["cat"])
    if params["mon"]:
        qs = qs.filter(moneda=params["mon"])

    return qs, params


@login_required
@user_passes_test(staff_required)
def exchange_dashboard(request):
    qs, params = _qs_exchange_from_request(request)

    cuenta = CuentaExchange.objects.first()

    # Caja del exchange = SOLO rentas del negocio (y ajustes si hubiera)
    RENTAS_CATS = ['spread_compra', 'spread_venta', 'fee_swap', 'ajuste']

    # Métricas globales (no tocan caja)
    FLOW_IN_CATS  = ['entrada']  # depósitos acreditados
    FLOW_OUT_CATS = ['salida']   # retiros enviados

    # KPIs de renta (en ARS normalizado)
    ingresos_ars = qs.filter(categoria__in=RENTAS_CATS).aggregate(s=Sum("monto_ars"))["s"] or Decimal("0")

    # Totales de flujo global (lo que entró y salió de la app)
    total_entradas = qs.filter(categoria__in=FLOW_IN_CATS).aggregate(s=Sum("monto_ars"))["s"] or Decimal("0")
    total_salidas  = qs.filter(categoria__in=FLOW_OUT_CATS).aggregate(s=Sum("monto_ars"))["s"] or Decimal("0")

    # Serie diaria (por defecto, sobre TODO lo filtrado). Si querés SOLO rentas:
    serie = (
        qs.filter(categoria__in=RENTAS_CATS)
          .annotate(dia=TruncDate("fecha"))
          .values("dia")
          .annotate(total_ars=Sum("monto_ars"))
          .order_by("dia")
    )
    serie_diaria = [{"dia": s["dia"].isoformat(), "total_ars": float(s["total_ars"] or 0)} for s in serie]

    totales_cat = (
        qs.values("categoria")
          .annotate(total_ars=Sum("monto_ars"), n=Count("id"))
          .order_by("-total_ars")
    )

    totales_mon = (
        qs.values("moneda")
          .annotate(total_mon=Sum("monto_moneda"), total_ars=Sum("monto_ars"))
          .order_by("moneda")
    )

    apuntes = qs.order_by("-fecha")[:200]

    ctx = {
        "params": params,
        "cuenta": cuenta,
        "ingresos_ars": ingresos_ars,      # renta neta normalizada a ARS
        "total_entradas": total_entradas,  # métrica global (depósitos)
        "total_salidas":  total_salidas,   # métrica global (retiros)
        "serie_diaria": serie_diaria,
        "totales_cat": totales_cat,
        "totales_mon": totales_mon,
        "apuntes": apuntes,
    }
    return render(request, "usuarios/exchange_dashboard.html", ctx)


@login_required
@user_passes_test(staff_required)
def exchange_export_csv(request):
    qs, params = _qs_exchange_from_request(request)

    # Armá la respuesta
    resp = HttpResponse(content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="exchange_apuntes.csv"'
    w = csv.writer(resp)

    # Encabezados (alineados a tu modelo nuevo)
    w.writerow([
        "fecha", "categoria", "moneda",
        "monto_moneda", "monto_ars",
        "usuario", "detalle",
        "ref_price", "applied_price",
        "ref_movimiento", "extra_json"
    ])

    for a in qs.order_by("fecha"):
        w.writerow([
            a.fecha.strftime("%Y-%m-%d %H:%M:%S"),
            a.categoria,
            a.moneda,
            f"{a.monto_moneda}",
            f"{a.monto_ars}",
            (a.usuario.username if a.usuario else ""),
            (a.detalle or ""),
            (a.ref_price or ""),
            (a.applied_price or ""),
            (a.ref_movimiento or ""),
            a.extra or {},
        ])

    return resp


@login_required
@user_passes_test(es_admin)
@require_POST
def guardar_config_exchange(request):
    cfg = ExchangeConfig.current()
    antes = {f: str(getattr(cfg, f)) for f in ExchangeConfigForm.Meta.fields}
    form = ExchangeConfigForm(request.POST, instance=cfg)
    if form.is_valid():
        form.instance.updated_by = request.user
        form.save()
        despues = {f: str(getattr(cfg, f)) for f in ExchangeConfigForm.Meta.fields}
        auditar(request.user, 'config_exchange', 'ExchangeConfig',
                **{f: f"{antes[f]} → {despues[f]}" for f in antes if antes[f] != despues[f]})
        # invalidar caché para que se refleje inmediato
        from django.core.cache import cache
        cache.delete("exchange_config_current")
        messages.success(request, "Configuración actualizada.")
    else:
        messages.error(request, "Revisá los valores ingresados.")
    return redirect('panel_admin')


@login_required
@user_passes_test(es_admin)
def exportar_movimientos_usuario_admin(request, user_id):
    """
    Exporta a CSV todos los movimientos de UN usuario específico,
    pensado para usar desde el panel admin (perfil del usuario).
    """
    usuario = get_object_or_404(Usuario, pk=user_id)
    movimientos = Movimiento.objects.filter(usuario=usuario).order_by('-fecha')

    response = HttpResponse(content_type='text/csv')
    filename = f"movimientos_{usuario.username}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(['Fecha', 'ID', 'Tipo', 'Moneda', 'Monto',
                     'Saldo antes', 'Saldo después', 'Descripción'])

    for m in movimientos:
        writer.writerow([
            localtime(m.fecha).strftime('%Y-%m-%d %H:%M'),
            m.codigo,
            m.tipo,
            m.moneda,
            m.monto,
            m.saldo_antes,
            m.saldo_despues,
            m.descripcion
        ])

    return response


def _accion_admin(request, accion, destino, exito=None):
    """Ejecuta una acción de services/fondos y avisa el resultado al admin."""
    try:
        accion()
        if exito:
            messages.success(request, exito)
    except OperacionError as e:
        messages.error(request, str(e))
    return redirect(destino)


@login_required
@user_passes_test(es_admin)
@require_POST
def aprobar_deposito(request, deposito_id):
    return _accion_admin(request, lambda: fondos.aprobar_deposito_ars(deposito_id, request.user),
                         'panel_depositos', "Depósito aprobado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def rechazar_deposito(request, deposito_id):
    return _accion_admin(request, lambda: fondos.rechazar_deposito(DepositoARS, deposito_id, request.user),
                         'panel_depositos', "Depósito rechazado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def aprobar_deposito_usdt(request, deposito_id):
    forzar = request.POST.get('forzar') == '1'
    return _accion_admin(request, lambda: fondos.aprobar_deposito_usdt(deposito_id, request.user, forzar=forzar),
                         'panel_depositos_usdt', "Depósito USDT aprobado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def reverificar_deposito_usdt(request, deposito_id):
    dep = get_object_or_404(DepositoUSDT, pk=deposito_id, estado='pendiente')
    res = onchain.verificar_deposito(dep)
    nivel = messages.success if res.estado == 'verificado' else messages.warning
    nivel(request, f"Depósito #{dep.pk}: {dep.get_verificacion_estado_display()}. {res.detalle}")
    return redirect('panel_depositos_usdt')


# ---------------- Compliance ----------------

@login_required
@user_passes_test(es_admin)
def compliance_alertas(request):
    estado = request.GET.get('estado', 'abierta')
    qs = AlertaCompliance.objects.select_related('usuario', 'revisada_por')
    if estado in dict(AlertaCompliance.ESTADOS):
        qs = qs.filter(estado=estado)
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'usuarios/compliance_alertas.html', {
        'page_obj': page, 'estado': estado, 'estados': AlertaCompliance.ESTADOS,
        'abiertas': AlertaCompliance.objects.filter(estado='abierta').count(),
    })


@login_required
@user_passes_test(es_admin)
@require_POST
def resolver_alerta(request, alerta_id):
    alerta = get_object_or_404(AlertaCompliance, pk=alerta_id, estado='abierta')
    estado = request.POST.get('estado')
    nota = (request.POST.get('nota') or '').strip()
    if estado not in ('descartada', 'reportada') or not nota:
        messages.error(request, "Elegí descartar o reportar e ingresá una nota con el fundamento.")
        return redirect('compliance_alertas')
    alerta.estado = estado
    alerta.nota = nota
    alerta.revisada_por = request.user
    alerta.revisada_en = timezone.now()
    alerta.save()
    auditar(request.user, f'alerta_{estado}', alerta, nota=nota, usuario=alerta.usuario_id)
    messages.success(request, f"Alerta #{alerta.pk} marcada como {alerta.get_estado_display().lower()}.")
    return redirect('compliance_alertas')


@login_required
@user_passes_test(es_admin)
def auditoria(request):
    qs = RegistroAuditoria.objects.select_related('actor')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(accion__icontains=q) | Q(objeto__icontains=q) | Q(actor__username__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'usuarios/auditoria.html', {'page_obj': page, 'q': q})


@login_required
@user_passes_test(es_admin)
@require_POST
def rechazar_deposito_usdt(request, deposito_id):
    return _accion_admin(request, lambda: fondos.rechazar_deposito(DepositoUSDT, deposito_id, request.user),
                         'panel_depositos_usdt', "Depósito USDT rechazado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def aprobar_retiro(request, id):
    return _accion_admin(request, lambda: fondos.aprobar_retiro_ars(id), 'panel_admin', "Retiro aprobado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def enviar_retiro(request, id):
    return _accion_admin(request, lambda: fondos.enviar_retiro_ars(id, request.user),
                         'panel_admin', "Retiro marcado como enviado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def rechazar_retiro_ars(request, id):
    return _accion_admin(request, lambda: fondos.rechazar_retiro_ars(id, request.user),
                         'panel_retiros', "Retiro rechazado y saldo recreditado.")


@login_required
@user_passes_test(es_admin)
@require_POST
def aprobar_retiro_cripto(request, id):
    return _accion_admin(
        request,
        lambda: fondos.enviar_retiro_crypto(id, request.user, red=request.POST.get('red', ''),
                                            txid=request.POST.get('txid', '')),
        'panel_admin', "Retiro aprobado y enviado.",
    )


@login_required
@user_passes_test(es_admin)
@require_POST
def rechazar_retiro_cripto(request, id):
    return _accion_admin(request, lambda: fondos.rechazar_retiro_crypto(id, request.user),
                         'panel_retiros', "Retiro rechazado y saldo recreditado.")
