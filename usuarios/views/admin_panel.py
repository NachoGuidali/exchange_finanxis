"""Panel de administración: KYC, aprobación de depósitos/retiros y contabilidad."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import Abs, TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import localtime
from django.views.decorators.http import require_POST

from usuarios.services import avisos, cotizaciones, fondos, onchain, soporte
from usuarios.services.auditoria import auditar
from usuarios.services.comun import OperacionError

from ..forms import CAMPOS_AVISOS, ExchangeConfigForm
from ..permisos import ROLES, pendientes_visibles, requiere
from ..utils import cotizacion_vigente, monto_csv, parse_monto
from ..models import (
    AlertaCompliance,
    ApunteExchange,
    BoletoOperacion,
    Cotizacion,
    CuentaExchange,
    DepositoARS,
    DepositoUSDT,
    ExchangeConfig,
    Movimiento,
    RetiroARS,
    RegistroAuditoria,
    RetiroCrypto,
    SupportTicket,
    Usuario,
)


@requiere('panel')
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
    # cada rol ve solo lo que puede resolver
    tipos = pendientes_visibles(request.user)
    cola = sorted((c for c in cola if c['tipo'] in tipos), key=lambda x: x['fecha'])

    filtro = request.GET.get('tipo', '')
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


@requiere('parametros')
def parametros_exchange(request):
    return _render_parametros(request)


def _render_parametros(request, form=None):
    cfg = ExchangeConfig.current()
    form = form or ExchangeConfigForm(instance=cfg)
    return render(request, 'usuarios/parametros_exchange.html', {
        'cfg': cfg, 'cfg_form': form,
        'avisos_campos': [form[c] for c in CAMPOS_AVISOS],
        'remitente': settings.DEFAULT_FROM_EMAIL,
        'servidor_email': avisos.estado_servidor(),
    })


@requiere('parametros')
@require_POST
def probar_email(request):
    try:
        avisos.probar_envio(request.user.email)
    except Exception as e:
        messages.error(request, f"No se pudo enviar el email de prueba: {e}")
    else:
        messages.success(request, f"Enviamos un email de prueba a {request.user.email}.")
    return redirect(reverse('parametros_exchange') + '#avisos')


DURACIONES_MANUAL = [('30', '30 minutos'), ('60', '1 hora'), ('240', '4 horas'), ('1440', '24 horas'),
                     ('4320', '3 días'), ('10080', '1 semana'),
                     ('0', 'Hasta que lo quite')]
NOMBRE_MONEDA = {'USDT': 'USDT', 'USD': 'Dólar'}


@requiere('cotizaciones')
def admin_cotizaciones(request):
    cfg = ExchangeConfig.current()
    monedas = []
    for moneda in cotizaciones.FUENTES:
        cot = cotizaciones.ultima(moneda)
        mercado = Cotizacion.objects.filter(moneda=moneda, manual=False).order_by('-fecha', '-id').first()
        monedas.append({
            'codigo': moneda,
            'nombre': NOMBRE_MONEDA[moneda],
            'cot': cot,
            'vigente': cotizacion_vigente(cot),
            'mercado': mercado,
            'spread_bps': cfg.spread_bps_usdt if moneda == 'USDT' else cfg.spread_bps_usd,
        })
    historial = Cotizacion.objects.select_related('cargada_por').order_by('-fecha', '-id')[:40]
    return render(request, 'usuarios/admin_cotizaciones.html', {
        'monedas': monedas,
        'historial': historial,
        'duraciones': DURACIONES_MANUAL,
        'max_edad': settings.COTIZACION_MAX_EDAD_MIN,
    })


@requiere('cotizaciones')
@require_POST
def actualizar_cotizaciones_admin(request):
    for moneda in cotizaciones.FUENTES:
        try:
            cot, fuente = cotizaciones.actualizar(moneda)
        except cotizaciones.PrecioManualActivo:
            messages.warning(request, f"{NOMBRE_MONEDA[moneda]}: tiene un precio manual activo, no se actualizó.")
        except cotizaciones.FuenteNoDisponible:
            messages.error(request, f"{NOMBRE_MONEDA[moneda]}: ninguna fuente respondió. Probá de nuevo en unos minutos.")
        else:
            auditar(request.user, 'cotizacion_actualizada', cot, moneda=moneda, fuente=fuente,
                    compra=cot.compra, venta=cot.venta)
            messages.success(request, f"{NOMBRE_MONEDA[moneda]} actualizado desde {fuente}.")
    return redirect('admin_cotizaciones')


@requiere('cotizaciones')
@require_POST
def fijar_precio_manual(request):
    moneda = request.POST.get('moneda')
    compra, venta = parse_monto(request.POST.get('compra')), parse_monto(request.POST.get('venta'))
    minutos = request.POST.get('duracion', '60')
    if moneda not in cotizaciones.FUENTES or minutos not in dict(DURACIONES_MANUAL):
        messages.error(request, "Datos inválidos.")
        return redirect('admin_cotizaciones')
    if compra is None or venta is None:
        messages.error(request, "Cargá precios de compra y venta válidos.")
        return redirect('admin_cotizaciones')
    hasta = timezone.now() + timedelta(minutes=int(minutos)) if minutos != '0' else None
    try:
        cot = cotizaciones.fijar_manual(moneda, compra, venta, hasta, request.user)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('admin_cotizaciones')
    auditar(request.user, 'cotizacion_manual', cot, moneda=moneda, compra=cot.compra, venta=cot.venta,
            hasta=hasta.isoformat() if hasta else 'hasta que la quiten')
    messages.success(request, f"Precio manual de {NOMBRE_MONEDA[moneda]} activo.")
    return redirect('admin_cotizaciones')


@requiere('cotizaciones')
@require_POST
def quitar_precio_manual(request):
    moneda = request.POST.get('moneda')
    if moneda not in cotizaciones.FUENTES:
        return redirect('admin_cotizaciones')
    cot = cotizaciones.quitar_manual(moneda)
    if cot is None:
        messages.info(request, f"{NOMBRE_MONEDA[moneda]} no tenía un precio manual activo.")
        return redirect('admin_cotizaciones')
    auditar(request.user, 'cotizacion_manual_quitada', cot, moneda=moneda)
    # vuelve enseguida al precio de mercado
    try:
        _, fuente = cotizaciones.actualizar(moneda)
        messages.success(request, f"{NOMBRE_MONEDA[moneda]} vuelve a la cotización automática ({fuente}).")
    except cotizaciones.FuenteNoDisponible:
        messages.warning(request, "Se quitó el precio manual, pero ninguna fuente respondió: compra y venta quedan "
                                  "pausadas hasta la próxima actualización.")
    return redirect('admin_cotizaciones')


@requiere('panel')
def admin_tickets(request):
    estado = request.GET.get('estado', 'abierto')
    q = request.GET.get('q', '').strip()
    qs = SupportTicket.objects.select_related('usuario').order_by('-actualizado_en')
    if estado in dict(SupportTicket.ESTADOS):
        qs = qs.filter(estado=estado)
    if q:
        filtro = (Q(asunto__icontains=q) | Q(mensaje__icontains=q) | Q(usuario__username__icontains=q) |
                  Q(usuario__first_name__icontains=q) | Q(usuario__last_name__icontains=q) |
                  Q(usuario__doc_nro__icontains=q) | Q(email__icontains=q))
        if q.lstrip('#').isdigit():
            filtro |= Q(pk=int(q.lstrip('#')))
        qs = qs.filter(filtro)
    conteo = dict(SupportTicket.objects.values_list('estado').annotate(n=Count('id')))
    return render(request, 'usuarios/admin_tickets.html', {
        'page_obj': Paginator(qs, 30).get_page(request.GET.get('page')),
        'estado': estado,
        'q': q,
        'estados': [(v, n, conteo.get(v, 0)) for v, n in SupportTicket.ESTADOS],
    })


@requiere('panel')
def admin_ticket(request, pk):
    ticket = get_object_or_404(SupportTicket.objects.select_related('usuario'), pk=pk)
    if request.method == 'POST':
        try:
            if request.POST.get('accion') == 'estado':
                soporte.cambiar_estado(ticket, request.POST.get('estado'), request.user)
                messages.success(request, f"Ticket #{ticket.pk}: {ticket.get_estado_display().lower()}.")
            else:
                soporte.responder(ticket, request.user, request.POST.get('mensaje'), es_staff=True)
                if request.POST.get('cerrar'):
                    soporte.cambiar_estado(ticket, 'cerrado', request.user)
                messages.success(request, "Respuesta enviada al cliente.")
        except ValueError as e:
            messages.error(request, str(e))
        return redirect('admin_ticket', pk=ticket.pk)
    return render(request, 'usuarios/admin_ticket.html', {
        'ticket': ticket,
        'respuestas': ticket.respuestas.select_related('autor'),
        'otros': SupportTicket.objects.filter(usuario=ticket.usuario).exclude(pk=ticket.pk).order_by('-creado_en')[:5],
        'estados': SupportTicket.ESTADOS,
    })


@requiere('equipo')
def admin_equipo(request):
    """Quién del equipo entra al panel y con qué rol. Solo superadmin."""
    roles = dict(ROLES)
    if request.method == 'POST':
        accion = request.POST.get('accion')
        ident = (request.POST.get('usuario') or '').strip()
        u = Usuario.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first()
        rol = request.POST.get('rol', '')
        if u is None:
            messages.error(request, f"No encontramos un usuario con «{ident}».")
        elif u.is_superuser:
            messages.error(request, "Los superusuarios de Django siempre son superadmin: no se cambian desde acá.")
        elif u == request.user:
            messages.error(request, "No podés cambiar tu propio rol.")
        elif accion == 'quitar':
            anterior = u.rol_staff
            u.is_staff, u.rol_staff = False, ''
            u.save(update_fields=['is_staff', 'rol_staff'])
            auditar(request.user, 'equipo_quitado', u, rol_anterior=anterior)
            messages.success(request, f"{u.username} ya no tiene acceso al panel.")
        elif rol in roles:
            anterior = u.rol_staff if u.is_staff else ''
            u.is_staff, u.rol_staff = True, rol
            u.save(update_fields=['is_staff', 'rol_staff'])
            auditar(request.user, 'equipo_rol', u, anterior=anterior, nuevo=rol)
            messages.success(request, f"{u.username}: {roles[rol].lower()}.")
        else:
            messages.error(request, "Elegí un rol.")
        return redirect('admin_equipo')

    equipo = Usuario.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).order_by('-is_superuser', 'rol_staff', 'username')
    return render(request, 'usuarios/admin_equipo.html', {
        'equipo': equipo,
        'roles': ROLES,
        'permisos_por_rol': [
            ('Operador', 'Transferencias, depósitos USDT, retiros y cotizaciones.'),
            ('Compliance', 'Identidades (KYC), alertas de compliance y auditoría.'),
            ('Contabilidad', 'Contabilidad y auditoría.'),
            ('Superadmin', 'Todo, más Parámetros y Equipo.'),
        ],
    })


@requiere('identidad')
def cambiar_estado_verificacion(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)

    if request.method == 'POST':
        nuevo_estado = request.POST.get('estado')
        if nuevo_estado == 'aprobado' and usuario.declaracion_vigente is None:
            messages.error(request, f"{usuario.get_full_name() or usuario.username} todavía no completó la "
                                    "declaración jurada (PEP y origen de fondos): no se puede aprobar.")
        elif nuevo_estado in ['pendiente', 'aprobado', 'rechazado']:
            anterior = usuario.estado_verificacion
            usuario.estado_verificacion = nuevo_estado
            if nuevo_estado == 'aprobado':
                usuario.is_active = True
            usuario.save()
            auditar(request.user, 'kyc_cambio_estado', usuario, anterior=anterior, nuevo=nuevo_estado)
            if nuevo_estado != anterior and nuevo_estado == 'aprobado':
                avisos.cliente(usuario, "Verificamos tu identidad: ya podés ingresar dinero y operar.",
                               asunto="Tu cuenta está habilitada", url=avisos.url_absoluta('agregar_saldo'),
                               boton="Ingresar dinero")
            elif nuevo_estado != anterior and nuevo_estado == 'rechazado':
                avisos.cliente(usuario, "No pudimos verificar tu identidad.", asunto="Verificación de identidad",
                               detalle="Escribinos a Soporte y te decimos qué falta para habilitar tu cuenta.",
                               url=avisos.url_absoluta('soporte'), boton="Escribir a Soporte")
            messages.success(request, f"{usuario.get_full_name() or usuario.username}: verificación {usuario.get_estado_verificacion_display().lower()}.")
    siguiente = request.POST.get('next', '')
    if siguiente and url_has_allowed_host_and_scheme(siguiente, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return redirect(siguiente)
    return redirect('panel_admin')


@requiere('fondos')
def panel_depositos(request):
    depositos = DepositoARS.objects.all().order_by('-fecha')
    return render(request, 'usuarios/panel_depositos.html', {'depositos':depositos})



@requiere('fondos')
def panel_retiros(request):
    retiros_ars = RetiroARS.objects.filter(estado='pendiente').order_by('-fecha_solicitud')
    retiros_crypto = RetiroCrypto.objects.filter(estado='pendiente').order_by('-fecha_solicitud')



    return render(request, 'usuarios/panel_retiros.html', {
        'retiros_ars': retiros_ars,
        'retiros_crypto': retiros_crypto,
    })


@requiere('fondos')
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


def _atajos_periodo(filtros, params, con_todo=False):
    """Links de período (Hoy, Últimos 7 días, ...) que conservan el resto de los filtros."""
    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)
    fin_mes_anterior = inicio_mes - timedelta(days=1)
    opciones = [('Hoy', hoy, hoy), ('Últimos 7 días', hoy - timedelta(days=6), hoy),
                ('Este mes', inicio_mes, hoy),
                ('Mes anterior', fin_mes_anterior.replace(day=1), fin_mes_anterior)]
    periodos = []
    for nombre, d, h in opciones:
        q = filtros.copy()
        q['desde'], q['hasta'] = d.isoformat(), h.isoformat()
        periodos.append({'nombre': nombre, 'query': q.urlencode(),
                         'activo': params['desde'] == d.isoformat() and params['hasta'] == h.isoformat()})
    if con_todo:
        q = filtros.copy()
        q.pop('desde', None)
        q.pop('hasta', None)
        periodos.append({'nombre': 'Todo', 'query': q.urlencode(),
                         'activo': not params['desde'] and not params['hasta']})
    return periodos


@requiere('panel')
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
    # comprobante de cada fila: el boleto cuelga de una pata; la otra pata de la operación lo comparte
    movs = list(page_obj.object_list)
    boletos = BoletoOperacion.objects.filter(
        Q(movimiento__in=movs) | Q(movimiento__operacion__in=[m.operacion for m in movs if m.operacion]),
        anulado=False).select_related('movimiento')
    por_mov = {b.movimiento_id: b for b in boletos}
    por_op = {b.movimiento.operacion: b for b in boletos if b.movimiento and b.movimiento.operacion}
    for m in movs:
        m.boleto = por_mov.get(m.pk) or por_op.get(m.operacion)
    page_obj.object_list = movs
    filtros = request.GET.copy()
    filtros.pop('page', None)

    sin_usuario = filtros.copy()
    for k in ('u', 'usuario'):
        sin_usuario.pop(k, None)

    periodos = _atajos_periodo(filtros, params)

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


@requiere('panel')
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
            monto_csv(m.monto, m.moneda),
            monto_csv(m.saldo_antes, m.moneda),
            monto_csv(m.saldo_despues, m.moneda),
            m.descripcion or '',
            m.admin_responsable.username if m.admin_responsable else '',
            m.codigo,
            m.operacion or '',
        ])
    return response



@requiere('panel')
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


@requiere('panel')
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


MONEDAS_RESUMEN = ('ARS', 'USDT', 'USD')
NOMBRES_CATEGORIA = {
    'spread_compra': 'Spread en compras', 'spread_venta': 'Spread en ventas', 'fee_swap': 'Comisión de swaps',
    'entrada': 'Depósitos acreditados', 'salida': 'Retiros enviados', 'ajuste': 'Ajustes',
}


def _por_moneda(filas, campo):
    d = {f['moneda']: f for f in filas}
    return [{'moneda': m, 'monto': abs(d[m][campo] or 0) if m in d else Decimal('0'),
             'n': d[m]['n'] if m in d else 0} for m in MONEDAS_RESUMEN]


def _resumen_plataforma(params):
    """
    Números grandes de la plataforma para el período (solo fechas; categoría y moneda filtran la tabla).
    Ingresos y retiros salen de los apuntes 'entrada'/'salida' (se crean al acreditar un depósito
    o enviar un retiro) y su valuación en pesos es la del momento en que ocurrieron.
    """
    d1, d2 = _parse_iso_date(params['desde']), _parse_iso_date(params['hasta'])

    def en_periodo(qs, campo='fecha'):
        if d1:
            qs = qs.filter(**{f'{campo}__date__gte': d1})
        if d2:
            qs = qs.filter(**{f'{campo}__date__lte': d2})
        return qs

    flujos = {}
    for cat in ('entrada', 'salida'):
        qs = en_periodo(ApunteExchange.objects.filter(categoria=cat))
        filas = qs.values('moneda').annotate(total=Sum('monto_moneda'), n=Count('id'))
        flujos[cat] = {
            'monedas': _por_moneda(filas, 'total'),
            'ars': abs(qs.aggregate(s=Sum('monto_ars'))['s'] or 0),
            'n': sum(f['n'] for f in filas),
        }
    neto_ars = flujos['entrada']['ars'] - flujos['salida']['ars']
    neto_monedas = [{'moneda': e['moneda'], 'monto': e['monto'] - s['monto']}
                    for e, s in zip(flujos['entrada']['monedas'], flujos['salida']['monedas'])]

    # en custodia: saldos actuales de los clientes + retiros pedidos que todavía no salieron
    saldos = Usuario.objects.aggregate(ARS=Sum('saldo_ars'), USDT=Sum('saldo_usdt'), USD=Sum('saldo_usd'))
    por_enviar = {m: Decimal('0') for m in MONEDAS_RESUMEN}
    for qs in (RetiroARS.objects.filter(estado__in=('pendiente', 'aprobado')),
               RetiroCrypto.objects.filter(estado='pendiente')):
        for f in qs.values('moneda').annotate(s=Sum('monto')):
            por_enviar[f['moneda']] = por_enviar.get(f['moneda'], 0) + (f['s'] or 0)
    custodia = [{'moneda': m, 'saldos': saldos[m] or Decimal('0'), 'por_enviar': por_enviar.get(m, 0),
                 'total': (saldos[m] or Decimal('0')) + por_enviar.get(m, 0)} for m in MONEDAS_RESUMEN]

    # volumen: patas en pesos de compras y ventas, y la moneda que entrega el cliente en los swaps
    operado = en_periodo(Movimiento.objects.filter(tipo__in=('compra', 'venta')))
    compra_venta = operado.filter(moneda='ARS').aggregate(total=Sum(Abs('monto')), n=Count('id'))
    swaps = operado.filter(descripcion__startswith='Swap', monto__lt=0).aggregate(total=Sum(Abs('monto')), n=Count('id'))

    ganancia = en_periodo(ApunteExchange.objects.filter(categoria__in=['spread_compra', 'spread_venta', 'fee_swap']))
    return {
        'ingresado': flujos['entrada'],
        'retirado': flujos['salida'],
        'neto_ars': neto_ars,
        'neto_monedas': neto_monedas,
        'custodia': custodia,
        'volumen_ars': compra_venta['total'] or Decimal('0'),
        'operaciones': compra_venta['n'] or 0,
        'volumen_swap': swaps['total'] or Decimal('0'),
        'swaps': swaps['n'] or 0,
        'ganancia_ars': ganancia.aggregate(s=Sum('monto_ars'))['s'] or Decimal('0'),
        'serie': _serie_flujos(d1, d2),
    }


def _serie_flujos(d1, d2):
    """Ingresos y retiros por día, en pesos. Sin período elegido, los últimos 30 días."""
    hasta = d2 or timezone.localdate()
    desde = d1 or (hasta - timedelta(days=29))
    if (hasta - desde).days > 366:
        desde = hasta - timedelta(days=366)
    filas = (ApunteExchange.objects.filter(categoria__in=('entrada', 'salida'),
                                           fecha__date__gte=desde, fecha__date__lte=hasta)
             .annotate(dia=TruncDate('fecha')).values('dia', 'categoria').annotate(s=Sum('monto_ars')))
    por_dia = {}
    for f in filas:
        por_dia.setdefault(f['dia'], {})[f['categoria']] = abs(float(f['s'] or 0))
    dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
    return {
        'labels': [d.isoformat() for d in dias],
        'entradas': [por_dia.get(d, {}).get('entrada', 0) for d in dias],
        'salidas': [por_dia.get(d, {}).get('salida', 0) for d in dias],
    }


@requiere('contabilidad')
def exchange_dashboard(request):
    qs, params = _qs_exchange_from_request(request)
    filtros = request.GET.copy()

    # caja del exchange = solo rentas del negocio (spread, comisiones y ajustes)
    rentas = ['spread_compra', 'spread_venta', 'fee_swap', 'ajuste']
    nombres = dict(ApunteExchange.CATEGORIAS)
    totales_cat = [{**t, 'nombre': NOMBRES_CATEGORIA.get(t['categoria'], nombres.get(t['categoria'], t['categoria']))}
                   for t in qs.values('categoria').annotate(total_ars=Sum('monto_ars'), n=Count('id'))
                   .order_by('-total_ars')]

    apuntes = list(qs.order_by("-fecha")[:200])
    for a in apuntes:
        a.nombre_categoria = NOMBRES_CATEGORIA.get(a.categoria, a.categoria)

    ctx = {
        "params": params,
        "resumen": _resumen_plataforma(params),
        "periodos": _atajos_periodo(filtros, params, con_todo=True),
        "cuenta": CuentaExchange.objects.first(),
        "renta_ars": qs.filter(categoria__in=rentas).aggregate(s=Sum("monto_ars"))["s"] or Decimal("0"),
        "totales_cat": totales_cat,
        "categorias": [(c, NOMBRES_CATEGORIA.get(c, n)) for c, n in ApunteExchange.CATEGORIAS],
        "apuntes": apuntes,
        "query_filtros": filtros.urlencode(),
    }
    return render(request, "usuarios/exchange_dashboard.html", ctx)


@requiere('contabilidad')
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


@requiere('parametros')
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
        messages.error(request, "Revisá los valores marcados.")
        return _render_parametros(request, form)
    return redirect('parametros_exchange')



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


@requiere('fondos')
@require_POST
def aprobar_deposito(request, deposito_id):
    return _accion_admin(request, lambda: fondos.aprobar_deposito_ars(deposito_id, request.user),
                         'panel_depositos', "Depósito aprobado.")


@requiere('fondos')
@require_POST
def rechazar_deposito(request, deposito_id):
    return _accion_admin(request, lambda: fondos.rechazar_deposito(DepositoARS, deposito_id, request.user),
                         'panel_depositos', "Depósito rechazado.")


@requiere('fondos')
@require_POST
def aprobar_deposito_usdt(request, deposito_id):
    forzar = request.POST.get('forzar') == '1'
    return _accion_admin(request, lambda: fondos.aprobar_deposito_usdt(deposito_id, request.user, forzar=forzar),
                         'panel_depositos_usdt', "Depósito USDT aprobado.")


@requiere('fondos')
@require_POST
def reverificar_deposito_usdt(request, deposito_id):
    dep = get_object_or_404(DepositoUSDT, pk=deposito_id, estado='pendiente')
    res = onchain.verificar_deposito(dep)
    nivel = messages.success if res.estado == 'verificado' else messages.warning
    nivel(request, f"Depósito #{dep.pk}: {dep.get_verificacion_estado_display()}. {res.detalle}")
    return redirect('panel_depositos_usdt')


# ---------------- Compliance ----------------

@requiere('compliance')
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


@requiere('compliance')
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


@requiere('auditoria')
def auditoria(request):
    qs = RegistroAuditoria.objects.select_related('actor')
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(accion__icontains=q) | Q(objeto__icontains=q) | Q(actor__username__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'usuarios/auditoria.html', {'page_obj': page, 'q': q})


@requiere('fondos')
@require_POST
def rechazar_deposito_usdt(request, deposito_id):
    return _accion_admin(request, lambda: fondos.rechazar_deposito(DepositoUSDT, deposito_id, request.user),
                         'panel_depositos_usdt', "Depósito USDT rechazado.")


@requiere('fondos')
@require_POST
def aprobar_retiro(request, id):
    return _accion_admin(request, lambda: fondos.aprobar_retiro_ars(id), 'panel_admin', "Retiro aprobado.")


@requiere('fondos')
@require_POST
def enviar_retiro(request, id):
    return _accion_admin(request, lambda: fondos.enviar_retiro_ars(
                         id, request.user, titular_confirmado=request.POST.get('titular') == '1'),
                         'panel_admin', "Retiro marcado como enviado.")


@requiere('fondos')
@require_POST
def rechazar_retiro_ars(request, id):
    return _accion_admin(request, lambda: fondos.rechazar_retiro_ars(id, request.user, request.POST.get('motivo')),
                         'panel_retiros', "Retiro rechazado y saldo recreditado.")


@requiere('fondos')
@require_POST
def aprobar_retiro_cripto(request, id):
    return _accion_admin(
        request,
        lambda: fondos.enviar_retiro_crypto(id, request.user, red=request.POST.get('red', ''),
                                            txid=request.POST.get('txid', '')),
        'panel_admin', "Retiro aprobado y enviado.",
    )


@requiere('fondos')
@require_POST
def rechazar_retiro_cripto(request, id):
    return _accion_admin(request, lambda: fondos.rechazar_retiro_crypto(id, request.user, request.POST.get('motivo')),
                         'panel_retiros', "Retiro rechazado y saldo recreditado.")
