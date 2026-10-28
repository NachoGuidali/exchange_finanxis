"""Dashboard, movimientos, exportaciones y comprobantes del usuario."""
import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponse
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.timezone import localdate, localtime

from ..models import (
    BoletoOperacion,
    Cotizacion,
    DepositoARS,
    DepositoUSDT,
    Movimiento,
    Notificacion,
    RetiroARS,
    RetiroCrypto,
)
from ..services import presentacion
from ..services.boletos import qr_base64
from ..utils import cotizacion_vigente, kyc_required


def _rate_ars(moneda, cot_usd, cot_usdt):
    """ARS por 1 unidad de moneda (aprox: usamos última cotización)."""
    if moneda == 'ARS':  return Decimal('1')
    if moneda == 'USD':  return Decimal(cot_usd.venta or cot_usd.compra or 0)
    if moneda == 'USDT': return Decimal(cot_usdt.venta or cot_usdt.compra or 0)
    return Decimal('0')


def _to_ars(monto, moneda, cot_usd, cot_usdt):
    return Decimal(monto or 0) * _rate_ars(moneda, cot_usd, cot_usdt)


def _rate_ars_compra(moneda, cot_usd, cot_usdt):
    """
    ARS por 1 unidad usando SIEMPRE precio de COMPRA (fallback a venta > 0).
    """
    if moneda == 'ARS':
        return Decimal('1')
    if moneda == 'USD':
        val = getattr(cot_usd, 'compra', None) or getattr(cot_usd, 'venta', 0)
        return Decimal(val or 0)
    if moneda == 'USDT':
        val = getattr(cot_usdt, 'compra', None) or getattr(cot_usdt, 'venta', 0)
        return Decimal(val or 0)
    return Decimal('0')


def _to_ars_compra(monto, moneda, cot_usd, cot_usdt):
    return Decimal(monto or 0) * _rate_ars_compra(moneda, cot_usd, cot_usdt)


@login_required
@kyc_required
def dashboard(request):

    cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
    cot_usd  = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
    vigente = cotizacion_vigente(cot_usdt) and cotizacion_vigente(cot_usd)
    # Placeholders si faltan cotizaciones
    if not cot_usdt: cot_usdt = type('X', (), {'compra':0,'venta':0})()
    if not cot_usd:  cot_usd  = type('X', (), {'compra':0,'venta':0})()

    actividades = presentacion.agrupar(Movimiento.objects.filter(usuario=request.user).order_by('-id')[:20])[:8]

    # KPIs normalizados a ARS con PRECIO COMPRA.
    # Salen de depósitos aprobados y retiros enviados (no de Movimiento, que
    # también tiene asientos informativos de solicitudes pendientes).
    u = request.user

    def _flujos(qs, campo_fecha, moneda=None):
        valores = ('dia',) if moneda else ('dia', 'moneda')
        filas = qs.annotate(dia=TruncDate(campo_fecha)).values(*valores).annotate(m=Sum('monto'))
        return [(f['dia'], moneda or f['moneda'], f['m']) for f in filas]

    flujos_dep = (
        _flujos(DepositoARS.objects.filter(usuario=u, estado='aprobado'), 'fecha', 'ARS') +
        _flujos(DepositoUSDT.objects.filter(usuario=u, estado='aprobado'), 'fecha', 'USDT')
    )
    flujos_ret = (
        _flujos(RetiroARS.objects.filter(usuario=u, estado='enviado'), 'fecha_actualizacion', 'ARS') +
        _flujos(RetiroCrypto.objects.filter(usuario=u, estado='enviado'), 'fecha_actualizacion')
    )

    total_ingresado_ars = sum((_to_ars_compra(m, mon, cot_usd, cot_usdt) for _, mon, m in flujos_dep), Decimal('0'))
    total_retirado_ars  = sum((_to_ars_compra(m, mon, cot_usd, cot_usdt) for _, mon, m in flujos_ret), Decimal('0'))

    flujo_neto_ars = total_ingresado_ars - total_retirado_ars

    # Valor de cartera actual a ARS con COMPRA
    cartera_ars = (
        _to_ars_compra(u.saldo_ars,  'ARS',  cot_usd, cot_usdt) +
        _to_ars_compra(u.saldo_usd,  'USD',  cot_usd, cot_usdt) +
        _to_ars_compra(u.saldo_usdt, 'USDT', cot_usd, cot_usdt)
    )

    # Serie diaria (últimos 30 días) con COMPRA
    hoy = localdate()
    desde = hoy - timedelta(days=29)

    mapa_dep, mapa_ret = {}, {}
    for dia, mon, m in flujos_dep:
        if dia >= desde:
            mapa_dep[dia] = mapa_dep.get(dia, Decimal('0')) + _to_ars_compra(m, mon, cot_usd, cot_usdt)
    for dia, mon, m in flujos_ret:
        if dia >= desde:
            mapa_ret[dia] = mapa_ret.get(dia, Decimal('0')) + _to_ars_compra(m, mon, cot_usd, cot_usdt)

    labels, data_dep, data_ret, data_net = [], [], [], []
    for i in range(30):
        d = desde + timedelta(days=i)
        dep = mapa_dep.get(d, Decimal('0'))
        ret = mapa_ret.get(d, Decimal('0'))
        labels.append(d.isoformat())
        data_dep.append(float(dep))
        data_ret.append(float(ret))
        data_net.append(float(dep - ret))

    # --- valores de compra para normalizar a ARS
    p_usdt = Decimal(cot_usdt.compra or 0)
    p_usd  = Decimal(cot_usd.compra  or 0)

    # datos para el donut (valor ARS por activo)
    comp_labels = ['ARS','USDT','USD']
    comp_units  = [
        float(Decimal(request.user.saldo_ars or 0)),
        float(Decimal(request.user.saldo_usdt or 0)),
        float(Decimal(request.user.saldo_usd  or 0)),
    ]
    comp_ars    = [
        float(Decimal(request.user.saldo_ars or 0)),
        float(Decimal(request.user.saldo_usdt or 0) * p_usdt),
        float(Decimal(request.user.saldo_usd  or 0) * p_usd),
    ]

    total_ars = sum(comp_ars) or 1
    composicion = [
        {'moneda': m, 'unidades': u, 'ars': a, 'pct': round(a * 100 / total_ars, 1)}
        for m, u, a in zip(comp_labels, comp_units, comp_ars)
    ]

    notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')[:10]
    notificaciones_no_leidas = Notificacion.objects.filter(usuario=request.user, leida=False)

    return render(request, 'usuarios/dashboard.html', {
        'actividades': actividades,
        'cot_usdt': cot_usdt,
        'cot_usd': cot_usd,
        # Bloque saldos usa request.user directamente (como pediste)
        # KPIs mini
        'kpi_total_ingresado': total_ingresado_ars,
        'kpi_total_retirado': total_retirado_ars,
        'kpi_flujo_neto': flujo_neto_ars,
        'kpi_cartera_ars': cartera_ars,
        # charts
        'chart_labels': labels,
        'chart_deps': data_dep,
        'chart_rets': data_ret,
        'chart_net':  data_net,
        'comp_labels': comp_labels,
        
        
        'comp_units':  comp_units,
        'comp_ars':    comp_ars,
        'composicion': composicion,
        'cotizacion_vigente': vigente,
        'notificaciones': notificaciones,
        'notificaciones_no_leidas': notificaciones_no_leidas,
    })


def _filtrar_actividades(request):
    """
    Filtros de la pantalla de movimientos. Fechas y búsqueda van a la base; tipo y moneda
    se aplican sobre las actividades ya agrupadas (un swap involucra dos monedas).
    """
    params = {
        'desde': request.GET.get('desde', ''),
        'hasta': request.GET.get('hasta', ''),
        'tipo': request.GET.get('tipo', ''),
        'moneda': request.GET.get('moneda', ''),
        'q': request.GET.get('q', '').strip(),
    }
    qs = Movimiento.objects.filter(usuario=request.user)
    d = parse_date(params['desde']) if params['desde'] else None
    h = parse_date(params['hasta']) if params['hasta'] else None
    if d:
        qs = qs.filter(fecha__date__gte=d)
    if h:
        qs = qs.filter(fecha__date__lte=h)
    if params['q']:
        qs = qs.filter(models.Q(descripcion__icontains=params['q']) | models.Q(codigo__icontains=params['q']))

    actividades = presentacion.agrupar(qs)
    if params['tipo'] in dict(presentacion.TIPOS):
        actividades = [a for a in actividades if a.tipo == params['tipo']]
    if params['moneda'] in ('ARS', 'USD', 'USDT'):
        actividades = [a for a in actividades if params['moneda'] in a.monedas]
    params['activos'] = any(params[k] for k in ('desde', 'hasta', 'tipo', 'moneda', 'q'))
    return actividades, params


@login_required
def mis_movimientos(request):
    actividades, params = _filtrar_actividades(request)

    netos = {}
    if params['activos']:
        for a in actividades:
            for m in a.movimientos:
                netos[m.moneda] = netos.get(m.moneda, Decimal('0')) + m.monto

    page_obj = Paginator(actividades, 30).get_page(request.GET.get('page'))
    filtros = request.GET.copy()
    filtros.pop('page', None)

    return render(request, 'usuarios/mis_movimientos.html', {
        'page_obj': page_obj,
        'params': params,
        'tipos': presentacion.TIPOS,
        'netos': [(m, netos[m]) for m in ('ARS', 'USDT', 'USD') if m in netos],
        'query_filtros': filtros.urlencode(),
    })


@login_required
def exportar_movimientos(request):
    actividades, _ = _filtrar_actividades(request)
    movimientos = sorted((m for a in actividades for m in a.movimientos), key=lambda m: m.fecha, reverse=True)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="movimientos.csv"'
    writer = csv.writer(response)
    writer.writerow(['Fecha', 'Tipo', 'Moneda', 'Monto', 'Saldo antes', 'Saldo después', 'Descripción', 'ID', 'Operación'])
    for m in movimientos:
        writer.writerow([
            localtime(m.fecha).strftime('%Y-%m-%d %H:%M:%S'),
            m.get_tipo_display(),
            m.moneda,
            f"{m.monto:.2f}",
            f"{m.saldo_antes:.2f}" if m.saldo_antes is not None else '',
            f"{m.saldo_despues:.2f}" if m.saldo_despues is not None else '',
            m.descripcion or '',
            m.codigo,
            m.operacion or '',
        ])
    return response


@login_required
def exportar_movimientos_usuario(request):
    movimientos = Movimiento.objects.filter(usuario=request.user).order_by('-fecha')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="movimientos_usuario.csv"'

    writer = csv.writer(response)
    writer.writerow(['Fecha','ID', 'Tipo', 'Moneda', 'Monto', 'Saldo antes', 'Saldo después', 'Descripción'])

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


@login_required
def comprobantes(request):
    boletas = (BoletoOperacion.objects.filter(usuario=request.user, anulado=False)
               .order_by('-fecha_emision'))
    page_obj = Paginator(boletas, 30).get_page(request.GET.get('page'))
    return render(request, "usuarios/comprobantes.html", {"page_obj": page_obj})


def _boleto_visible(request, numero):
    """El cliente ve solo sus comprobantes; el staff, los de todos."""
    qs = BoletoOperacion.objects.select_related('usuario', 'movimiento')
    if not request.user.is_staff:
        qs = qs.filter(usuario=request.user)
    return get_object_or_404(qs, numero=numero)


# tipo de boleto → cómo se nombran los montos en pantalla
CLASE_BOLETO = {'compra': 'operacion', 'venta': 'operacion', 'swap': 'operacion',
                'deposito': 'deposito', 'retiro': 'retiro'}


@login_required
def ver_comprobante(request, numero):
    b = _boleto_visible(request, numero)
    s = b.snapshot or {}
    ver_url = request.build_absolute_uri(reverse('verificar_boleto', args=[b.numero]))
    tasa = s.get('tasa_fmt')
    return render(request, "usuarios/comprobante.html", {
        "b": b,
        "s": s,
        "clase": CLASE_BOLETO.get(b.tipo.split('_')[0], 'operacion'),
        "tasa": tasa if tasa and tasa != '—' else '',
        "ver_url": ver_url,
        "qr_base64": qr_base64(ver_url),
        "es_admin": request.user.is_staff,
    })


@login_required
def descargar_boleto(request, numero):
    b = _boleto_visible(request, numero)
    if not b.pdf:
        messages.info(request, "Este comprobante no tiene PDF. Podés descargarlo como imagen.")
        return redirect('ver_comprobante', numero=b.numero)
    return FileResponse(b.pdf.open("rb"), as_attachment=True, filename=f"{b.numero}.pdf")
