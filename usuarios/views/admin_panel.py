"""Panel de administración: KYC, aprobación de depósitos/retiros y contabilidad."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import localtime
from django.views.decorators.http import require_POST

from usuarios.services import fondos, onchain
from usuarios.services.auditoria import auditar
from usuarios.services.comun import OperacionError

from ..forms import ExchangeConfigForm
from ..utils import parse_monto
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
    """Pendientes: todo lo que espera una decisión del operador, del más viejo al más nuevo."""
    q = (request.GET.get('q') or '').strip()
    if q:
        return redirect(f"{reverse('admin_usuarios_list')}?{urlencode({'q': q})}")

    cola = []
    for u in Usuario.objects.filter(estado_verificacion='pendiente'):
        cola.append({'tipo': 'kyc', 'fecha': u.date_joined, 'usuario': u, 'obj': u})
    for d in DepositoARS.objects.filter(estado='pendiente').select_related('usuario'):
        cola.append({'tipo': 'deposito_ars', 'fecha': d.fecha, 'usuario': d.usuario, 'obj': d})
    for d in DepositoUSDT.objects.filter(estado='pendiente').select_related('usuario'):
        cola.append({'tipo': 'deposito_usdt', 'fecha': d.fecha, 'usuario': d.usuario, 'obj': d})
    for r in RetiroARS.objects.filter(estado__in=('pendiente', 'aprobado')).select_related('usuario'):
        cola.append({'tipo': 'retiro_ars', 'fecha': r.fecha_solicitud, 'usuario': r.usuario, 'obj': r})
    for r in RetiroCrypto.objects.filter(estado='pendiente').select_related('usuario'):
        cola.append({'tipo': 'retiro_crypto', 'fecha': r.fecha_solicitud, 'usuario': r.usuario, 'obj': r})
    for al in AlertaCompliance.objects.filter(estado='abierta').select_related('usuario'):
        cola.append({'tipo': 'alerta', 'fecha': al.fecha, 'usuario': al.usuario, 'obj': al})
    cola.sort(key=lambda x: x['fecha'])

    filtro = request.GET.get('tipo', '')
    tipos = {'kyc', 'deposito_ars', 'deposito_usdt', 'retiro_ars', 'retiro_crypto', 'alerta'}
    if filtro in tipos:
        cola = [c for c in cola if c['tipo'] == filtro]

    hoy = timezone.localdate()
    resumen_hoy = {
        'operaciones': Movimiento.objects.filter(fecha__date=hoy, tipo__in=('compra', 'venta'), monto__gt=0).count(),
        'usuarios_nuevos': Usuario.objects.filter(date_joined__date=hoy).count(),
    }
    return render(request, 'usuarios/panel_admin.html', {
        'cola': cola, 'filtro': filtro, 'resumen_hoy': resumen_hoy,
    })


@login_required
@user_passes_test(es_admin)
def parametros_exchange(request):
    cfg = ExchangeConfig.current()
    return render(request, 'usuarios/parametros_exchange.html', {
        'cfg': cfg, 'cfg_form': ExchangeConfigForm(instance=cfg),
    })


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
            messages.success(request, f"{usuario.get_full_name() or usuario.username}: verificación {usuario.get_estado_verificacion_display().lower()}.")
    siguiente = request.POST.get('next', '')
    if siguiente and url_has_allowed_host_and_scheme(siguiente, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return redirect(siguiente)
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


def _filtrar_movimientos_admin(request):
    """
    Filtros de la pantalla de movimientos del admin, compartidos con el CSV.
    Montos: sobre el valor absoluto, así "desde 100.000" trae entradas y salidas de ese tamaño.
    """
    p = {k: request.GET.get(k, '').strip() for k in
         ('desde', 'hasta', 'usuario', 'u', 'moneda', 'tipo', 'sentido', 'min', 'max', 'q')}
    qs = Movimiento.objects.select_related('usuario', 'admin_responsable')

    d, h = _parse_iso_date(p['desde']), _parse_iso_date(p['hasta'])
    if d:
        qs = qs.filter(fecha__date__gte=d)
    if h:
        qs = qs.filter(fecha__date__lte=h)

    filtrado_usuario = None
    if p['u'].isdigit():
        filtrado_usuario = Usuario.objects.filter(pk=p['u']).first()
        qs = qs.filter(usuario_id=p['u'])
    elif p['usuario']:
        t = p['usuario']
        qs = qs.filter(Q(usuario__username__icontains=t) | Q(usuario__email__icontains=t) |
                       Q(usuario__first_name__icontains=t) | Q(usuario__last_name__icontains=t) |
                       Q(usuario__doc_nro__icontains=t))

    if p['moneda'] in dict(Movimiento.MONEDA_CHOICES):
        qs = qs.filter(moneda=p['moneda'])
    if p['tipo'] in dict(Movimiento.TIPO_CHOICES):
        qs = qs.filter(tipo=p['tipo'])
    if p['sentido'] == 'entrada':
        qs = qs.filter(monto__gt=0)
    elif p['sentido'] == 'salida':
        qs = qs.filter(monto__lt=0)

    minimo, maximo = parse_monto(p['min']), parse_monto(p['max'])
    if minimo is not None:
        qs = qs.filter(Q(monto__gte=minimo) | Q(monto__lte=-minimo))
    if maximo is not None:
        qs = qs.filter(monto__lte=maximo, monto__gte=-maximo)

    if p['q']:
        qs = qs.filter(Q(descripcion__icontains=p['q']) | Q(codigo__icontains=p['q']) |
                       Q(operacion__icontains=p['q']))

    p['activos'] = any(p[k] for k in ('desde', 'hasta', 'usuario', 'u', 'moneda', 'tipo', 'sentido', 'min', 'max', 'q'))
    p['filtrado_usuario'] = filtrado_usuario
    return qs.order_by('-fecha', '-id'), p


@login_required
@user_passes_test(es_admin)
def admin_movimientos(request):
    qs, params = _filtrar_movimientos_admin(request)

    totales = []
    for t in qs.order_by().values('moneda').annotate(
            n=Count('id'),
            entradas=Sum('monto', filter=Q(monto__gt=0)),
            salidas=Sum('monto', filter=Q(monto__lt=0)),
            neto=Sum('monto')):
        totales.append({**t, 'entradas': t['entradas'] or Decimal('0'), 'salidas': -(t['salidas'] or Decimal('0'))})
    orden = {'ARS': 0, 'USDT': 1, 'USD': 2}
    totales.sort(key=lambda t: orden.get(t['moneda'], 9))

    page_obj = Paginator(qs, 50).get_page(request.GET.get('page'))
    filtros = request.GET.copy()
    filtros.pop('page', None)

    sin_usuario = filtros.copy()
    for k in ('u', 'usuario'):
        sin_usuario.pop(k, None)

    # atajos de período que conservan el resto de los filtros
    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)
    fin_mes_anterior = inicio_mes - timedelta(days=1)
    periodos = []
    for nombre, d, h in (('Hoy', hoy, hoy), ('Últimos 7 días', hoy - timedelta(days=6), hoy),
                         ('Este mes', inicio_mes, hoy),
                         ('Mes anterior', fin_mes_anterior.replace(day=1), fin_mes_anterior)):
        q = filtros.copy()
        q['desde'], q['hasta'] = d.isoformat(), h.isoformat()
        periodos.append({'nombre': nombre, 'query': q.urlencode(),
                         'activo': params['desde'] == d.isoformat() and params['hasta'] == h.isoformat()})

    return render(request, 'usuarios/admin_movimientos.html', {
        'periodos': periodos,
        'page_obj': page_obj,
        'params': params,
        'totales': totales,
        'cantidad': sum(t['n'] for t in totales),
        'tipos': Movimiento.TIPO_CHOICES,
        'monedas': Movimiento.MONEDA_CHOICES,
        'query_filtros': filtros.urlencode(),
        'query_sin_usuario': sin_usuario.urlencode(),
    })


@login_required
@user_passes_test(es_admin)
def exportar_movimientos_admin(request):
    qs, params = _filtrar_movimientos_admin(request)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    nombre = f"movimientos_{timezone.localdate():%Y%m%d}.csv"
    response['Content-Disposition'] = f'attachment; filename="{nombre}"'
    response.write('\ufeff')  # BOM: Excel abre bien los acentos
    writer = csv.writer(response)
    writer.writerow(['Fecha', 'Usuario', 'Nombre', 'DNI', 'Email', 'Tipo', 'Moneda', 'Monto',
                     'Saldo antes', 'Saldo después', 'Descripción', 'Admin', 'ID', 'Operación'])
    for m in qs.iterator(chunk_size=2000):
        u = m.usuario
        writer.writerow([
            localtime(m.fecha).strftime('%Y-%m-%d %H:%M:%S'),
            u.username,
            u.get_full_name(),
            u.doc_nro or '',
            u.email,
            m.get_tipo_display(),
            m.moneda,
            f"{m.monto:.2f}",
            f"{m.saldo_antes:.2f}" if m.saldo_antes is not None else '',
            f"{m.saldo_despues:.2f}" if m.saldo_despues is not None else '',
            m.descripcion or '',
            m.admin_responsable.username if m.admin_responsable else '',
            m.codigo,
            m.operacion or '',
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
        messages.success(request, "Parámetros guardados.")
    else:
        messages.error(request, "Revisá los valores ingresados.")
    return redirect('parametros_exchange')


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
    """Ejecuta una acción de services/fondos y avisa el resultado al admin.
    Vuelve a `next` (p. ej. la cola de pendientes) si es una URL interna."""
    try:
        accion()
        if exito:
            messages.success(request, exito)
    except OperacionError as e:
        messages.error(request, str(e))
    siguiente = request.POST.get('next', '')
    if siguiente and url_has_allowed_host_and_scheme(siguiente, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return redirect(siguiente)
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
