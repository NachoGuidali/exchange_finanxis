from django.shortcuts import render, redirect, get_object_or_404, HttpResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponseRedirect, FileResponse, HttpResponse
from .forms import RegistroUsuarioForm, DepositoARSForm, DepositoUSDTForm, SupportTicketForm
from django.contrib import messages
from django.urls import reverse
from .models import Usuario, DepositoARS, Movimiento, Cotizacion, RetiroARS, Notificacion, RetiroCrypto, DepositoUSDT, BoletoOperacion, Provincia, Localidad, Pais, SupportTicket, ApunteExchange, CuentaExchange
from decimal import Decimal, ROUND_DOWN
from django.views.decorators.http import require_GET, require_POST
from django.http import JsonResponse
import logging
import csv
from django.db.models.functions import TruncDate, TruncMonth
from django.db.models import Sum, Case, When, Value, DecimalField, Q, Count, F
from datetime import datetime, timedelta, date
from django.utils.timezone import localtime, localdate, now
from .utils import registrar_movimiento, crear_notificacion, cliente_ctx
from django.db import transaction
from django.conf import settings
from django.contrib.auth import logout, update_session_auth_hash, get_user_model, login
from usuarios.services.boletos import emitir_boleto
from django.core.paginator import Paginator
from django.utils.dateparse import parse_date
from django.db import models
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
import uuid
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import smart_str, force_bytes
from usuarios.services.accounting import (
    registrar_spread_compra, registrar_spread_venta, registrar_comision_swap
)
from django.utils import timezone
from .utils_email_verify import send_verification_email, build_verification_link
from django.contrib.auth.views import LoginView
from django.contrib import messages
from .forms import EmailOrUsernameAuthenticationForm


logger = logging.getLogger(__name__)
User = get_user_model()

# helpers de formato / numeración
def q2(x): return Decimal(x).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
def fmt_money(x, symbol='$'): return f"{symbol}{q2(Decimal(x)):,.2f}"
def fmt_ccy(x, ccy): return f"{q2(Decimal(x))} {ccy}"
def gen_numero_boleto(): return f"BOL-{localtime().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

def home(request):
    return render(request, "home.html")


class LoginViewCustom(LoginView):
    authentication_form = EmailOrUsernameAuthenticationForm
    template_name = 'usuarios/login.html'

    def form_valid(self, form):
        user = form.get_user()
        # ModelBackend ya bloquea is_active=False, pero podés dar feedback:
        if not user.is_active:
            messages.error(self.request, "Tu email no está verificado. Revisá tu bandeja o reenviá el correo.")
            return self.form_invalid(form)
        return super().form_valid(form) 
    

#INICIO REGISTRO NUEVO

def registro(request):
    if request.method == 'POST':
        form = RegistroUsuarioForm(request.POST, request.FILES)
        if form.is_valid():
            # 1) Crear instancia sin guardar para poder setear flags
            user = form.save(commit=False)

            # 2) Bloquear login hasta verificar email
            user.is_active = True

            # (Opcional, si usás KYC y estados propios)
            user.estado_verificacion = 'pendiente'

            # 3) Timestamp de cuándo enviamos el correo (útil para rate-limit reenvíos)
            user.email_confirm_sent_at = timezone.now()

            # 4) Guardar definitivamente
            user.save()

            # 5) Enviar email de verificación (PASAR LA INSTANCIA)
            send_verification_email(request, user)

            # 6) Aviso + redirección a pantalla “revisá tu correo”
            messages.success(
                request,
                'Registro creado. Te enviamos un correo para verificar tu email.'
            )
            return redirect('verify_email_notice')
    else:
        form = RegistroUsuarioForm()

    return render(request, 'usuarios/registro.html', {'form': form})


@require_GET
def geo_provincias(request):
    pais_id = request.GET.get("pais_id")
    if not pais_id:
        return JsonResponse({"provincias": []})
    qs = Provincia.objects.filter(pais_id=pais_id).order_by("nombre")
    data = [{"id": p.id, "nombre": p.nombre} for p in qs]
    return JsonResponse({"provincias": data})

@require_GET
def geo_localidades(request):
    prov_id = request.GET.get("provincia_id")
    if not prov_id:
        return JsonResponse({"localidades": []})
    qs = Localidad.objects.filter(provincia_id=prov_id).order_by("nombre")
    data = [{"id": l.id, "nombre": l.nombre} for l in qs]
    return JsonResponse({"localidades": data})




@login_required
@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada correctamente.")
    return redirect('login')

# @login_required
# def dashboard(request):
#     if request.user.estado_verificacion != 'aprobado':
#         return render(request, 'usuarios/no_verificado.html')
    
#     movimientos = Movimiento.objects.filter(usuario=request.user).order_by('-fecha')[:10]

#     cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
#     cot_usd = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
#     notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')[:10]
#     notificaciones_no_leidas = Notificacion.objects.filter(usuario=request.user, leida=False)

#     return render(request, 'usuarios/dashboard.html', {
#         'movimientos': movimientos,
#         'cot_usdt': cot_usdt,
#         'cot_usd': cot_usd,
#         'notificaciones': notificaciones,
#         'notificaciones_no_leidas': notificaciones_no_leidas,
#     })

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
def dashboard(request):
    if not request.user.email_confirmed:
        return redirect('verify_email_notice')
    if request.user.estado_verificacion != 'aprobado':
        return render(request, 'usuarios/no_verificado.html')

    cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
    cot_usd  = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
    # Placeholders si faltan cotizaciones
    if not cot_usdt: cot_usdt = type('X', (), {'compra':0,'venta':0})()
    if not cot_usd:  cot_usd  = type('X', (), {'compra':0,'venta':0})()

    movimientos = Movimiento.objects.filter(usuario=request.user).order_by('-fecha')[:10]

    # KPIs normalizados a ARS con PRECIO COMPRA
    base_qs = Movimiento.objects.filter(usuario=request.user)

    total_ingresado_ars = Decimal('0')
    for mon in ('ARS','USD','USDT'):
        s = base_qs.filter(tipo='deposito', moneda=mon).aggregate(s=Sum('monto'))['s'] or 0
        total_ingresado_ars += _to_ars_compra(s, mon, cot_usd, cot_usdt)

    total_retirado_ars = Decimal('0')
    for mon in ('ARS','USD','USDT'):
        s = base_qs.filter(tipo='retiro', moneda=mon).aggregate(s=Sum('monto'))['s'] or 0
        total_retirado_ars += _to_ars_compra(s, mon, cot_usd, cot_usdt)

    flujo_neto_ars = total_ingresado_ars - total_retirado_ars

    # Valor de cartera actual a ARS con COMPRA
    cartera_ars = (
        _to_ars_compra(request.user.saldo_ars,  'ARS',  cot_usd, cot_usdt) +
        _to_ars_compra(request.user.saldo_usd,  'USD',  cot_usd, cot_usdt) +
        _to_ars_compra(request.user.saldo_usdt, 'USDT', cot_usd, cot_usdt)
    )

    # Serie diaria (últimos 30 días) con COMPRA
    hoy = date.today()
    desde = hoy - timedelta(days=29)

    serie_deps = (
        base_qs.filter(tipo='deposito', fecha__date__gte=desde)
               .annotate(dia=TruncDate('fecha'))
               .values('dia','moneda')
               .annotate(m=Sum('monto'))
    )
    serie_rets = (
        base_qs.filter(tipo='retiro', fecha__date__gte=desde)
               .annotate(dia=TruncDate('fecha'))
               .values('dia','moneda')
               .annotate(m=Sum('monto'))
    )

    mapa_dep, mapa_ret = {}, {}
    for r in serie_deps:
        k = r['dia']
        mapa_dep.setdefault(k, Decimal('0'))
        mapa_dep[k] += _to_ars_compra(r['m'], r['moneda'], cot_usd, cot_usdt)
    for r in serie_rets:
        k = r['dia']
        mapa_ret.setdefault(k, Decimal('0'))
        mapa_ret[k] += _to_ars_compra(r['m'], r['moneda'], cot_usd, cot_usdt)

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

    notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')[:10]
    notificaciones_no_leidas = Notificacion.objects.filter(usuario=request.user, leida=False)

    return render(request, 'usuarios/dashboard.html', {
        'movimientos': movimientos,
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
        'notificaciones': notificaciones,
        'notificaciones_no_leidas': notificaciones_no_leidas,
    })

def es_admin(user):
    return user.is_superuser or user.is_staff


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
    return render(request, 'usuarios/panel_admin.html', context)

@login_required
@user_passes_test(es_admin)
def cambiar_estado_verificacion(request, user_id):
    usuario = get_object_or_404(Usuario, id=user_id)

    if request.method == 'POST':
        nuevo_estado = request.POST.get('estado')
        if nuevo_estado in ['pendiente', 'aprobado', 'rechazado']:
            usuario.estado_verificacion = nuevo_estado
            if nuevo_estado == 'aprobado':
                usuario.is_active = True
            usuario.save()
    return redirect('panel_admin')


@login_required
def agregar_saldo(request):
    if request.method == 'POST':
        form = DepositoARSForm(request.POST, request.FILES)
        if form.is_valid():
            deposito = form.save(commit=False)
            deposito.usuario = request.user
            deposito.estado = 'pendiente'
            deposito.save()

            saldo_actual = request.user.saldo_ars
            registrar_movimiento(
                usuario=request.user,
                tipo='deposito',
                moneda='ARS',
                monto=deposito.monto,
                descripcion='Solicitud de depósito enviada. En revisión.',
                saldo_antes=saldo_actual,
                saldo_despues=saldo_actual
            )
            messages.success(request, 'Solicitud enviada. En breve será verificada')
            return redirect('dashboard')
    else:
        form = DepositoARSForm()

    datos_bancarios = {
        'alias' : 'alias.usuario',
        'cbu' : '0000003100000001234567',
        'banco' : 'banco',
    }        

    return render(request, 'usuarios/agregar_saldo.html', {
        'form': form,
        'datos_bancarios' : datos_bancarios
    })


@login_required
def depositar_usdt(request):
    if request.user.estado_verificacion != 'aprobado':
        return render(request, 'usuarios/no_verificado.html')

    if request.method == 'POST':
        form = DepositoUSDTForm(request.POST, request.FILES)
        if form.is_valid():
            dep = form.save(commit=False)
            dep.usuario = request.user
            dep.estado = 'pendiente'
            dep.save()

            # Movimiento informativo (no cambia saldos)
            registrar_movimiento(
                usuario=request.user,
                tipo='deposito',
                moneda='USDT',
                monto=0,
                descripcion=f"Solicitud de depósito USDT enviada (monto: {dep.monto}, red: {dep.red}, txid: {dep.txid}). En revisión.",
                saldo_antes=request.user.saldo_usdt,
                saldo_despues=request.user.saldo_usdt
            )
            messages.success(request, 'Solicitud enviada. En breve será verificada.')
            return redirect('dashboard')
    else:
        form = DepositoUSDTForm()

    # opcional: mostrar tu wallet, red recomendada, etc.
    datos_wallet = {
        'wallet_trc20': 'TU_WALLET_TRC20',
        'wallet_erc20': 'TU_WALLET_ERC20',
    }

    return render(request, 'usuarios/depositar_usdt.html', {
        'form': form,
        'datos_wallet': datos_wallet
    })


@login_required
@user_passes_test(es_admin)
def panel_depositos(request):
    depositos = DepositoARS.objects.all().order_by('-fecha')
    return render(request, 'usuarios/panel_depositos.html', {'depositos':depositos})

@login_required
@user_passes_test(es_admin)
def aprobar_deposito(request, deposito_id):
    from usuarios.services.boletos import emitir_boleto
    deposito = get_object_or_404(DepositoARS, id=deposito_id)
    if request.method == 'POST' and deposito.estado == 'pendiente':
        with transaction.atomic():
            deposito.estado = 'aprobado'
            deposito.save()

            u = Usuario.objects.select_for_update().get(pk=deposito.usuario_id)
            antes = u.saldo_ars
            u.saldo_ars = q2(u.saldo_ars + deposito.monto)
            u.save()
            despues = u.saldo_ars

            mov = registrar_movimiento(
                usuario=u, tipo='deposito', moneda='ARS', monto=deposito.monto,
                descripcion='Depósito aprobado por admin',
                admin=request.user, saldo_antes=antes, saldo_despues=despues
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': 'Acreditación de depósito ARS',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_money(deposito.monto, '$'),
                'comision_total_fmt': None,
                'monto_origen_fmt': fmt_money(deposito.monto, '$'),
                'tasa_fmt': '—',
                'monto_destino_fmt': fmt_money(deposito.monto, '$'),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            emitir_boleto(u, 'deposito_ars', numero, snapshot, movimiento=mov)

        crear_notificacion(u, f"Tu depósito de ${deposito.monto} ARS fue aprobado.")
    return redirect('panel_depositos')

 





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
def rechazar_deposito(request, deposito_id):
    deposito = get_object_or_404(DepositoARS, id=deposito_id)
    if deposito.estado == 'pendiente':
        deposito.estado = 'rechazado'
        deposito.save()
        registrar_movimiento(
            usuario=deposito.usuario,
            tipo='ajuste',
            moneda='ARS',
            monto=0,
            descripcion=f'Depósito rechazado por admin. Monto solicitado: ${deposito.monto}'
        )
    return redirect('panel_depositos')





# @login_required
# def operar(request):
#     # Obtener última cotización ya con comisión aplicada
#     cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
#     cot_usd = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()

#     if not cot_usdt or not cot_usd:
#         return HttpResponse("No hay cotización disponible. Intentá más tarde.", status=503)

#     # Usar directamente los valores de la BD (ya tienen comisión aplicada)
#     cot_usdt_compra = cot_usdt.compra
#     cot_usdt_venta = cot_usdt.venta
#     cot_usd_compra = cot_usd.compra
#     cot_usd_venta = cot_usd.venta

#     if request.method == 'POST':
#         operacion = request.POST.get('operacion')
#         moneda = request.POST.get('moneda')

#         try:
#             monto = Decimal(request.POST.get('monto'))

#             if operacion == 'compra':
#                 cot = cot_usdt_venta if moneda == 'USDT' else cot_usd_venta
#                 exito, error = procesar_compra(request.user, moneda, monto, cot)
#             elif operacion == 'venta':
#                 cot = cot_usdt_compra if moneda == 'USDT' else cot_usd_compra
#                 exito, error = procesar_venta(request.user, moneda, monto, cot)
#             else:
#                 return HttpResponse("Operación no válida", status=400)

#             if not exito:
#                 return HttpResponse(error, status=400)

#             return redirect('dashboard')

#         except Exception as e:
#             logger.error(f"[OPERAR ERROR] Usuario: {request.user.username} - Error: {str(e)}")
#             return HttpResponse("Ocurrió un error al procesar la operación.", status=400)

#     return render(request, 'usuarios/operar.html', {
#         'cot_usdt': {'compra': cot_usdt_compra, 'venta': cot_usdt_venta},
#         'cot_usd': {'compra': cot_usd_compra, 'venta': cot_usd_venta},
#     })





@login_required
def operar(request):
    if request.user.estado_verificacion != 'aprobado':
        return render(request, 'usuarios/no_verificado.html')

    cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
    cot_usd  = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
    if not cot_usdt or not cot_usd:
        return HttpResponse("No hay cotización disponible. Intentá más tarde.", status=503)

    # precios PUBLICADOS (applied)
    cot_usdt_compra = cot_usdt.compra
    cot_usdt_venta  = cot_usdt.venta
    cot_usd_compra  = cot_usd.compra
    cot_usd_venta   = cot_usd.venta

    if request.method == 'POST':
        operacion = request.POST.get('operacion')

        if operacion in ('compra', 'venta'):
            moneda = request.POST.get('moneda')
            try:
                monto = Decimal(request.POST.get('monto'))
            except Exception:
                return HttpResponse("Monto inválido.", status=400)

            try:
                if operacion == 'compra':
                    # cliente paga ARS y recibe CCY al precio de VENTA publicado
                    if moneda == 'USDT':
                        cot_applied = Decimal(cot_usdt_venta); ref_price = Decimal(cot_usdt.ref_venta or cot_usdt_venta)
                    else:
                        cot_applied = Decimal(cot_usd_venta);  ref_price = Decimal(cot_usd.ref_venta  or cot_usd_venta)
                    ok, err, mov_ccy = procesar_compra(request.user, moneda, monto, cot_applied, return_mov_ccy=True)
                    if not ok: return HttpResponse(err, status=400)
                    # ingreso de la casa por spread (en ARS)
                    registrar_spread_compra(
                        usuario=request.user, moneda_ccy=moneda, monto_ars=monto,
                        ref_price=ref_price, applied_price=cot_applied,
                        movimiento=mov_ccy, detalle_extra={"cot_id": (cot_usdt.id if moneda=='USDT' else cot_usd.id)}
                    )
                else:
                    # cliente entrega CCY y recibe ARS al precio de COMPRA publicado
                    if moneda == 'USDT':
                        cot_applied = Decimal(cot_usdt_compra); ref_price = Decimal(cot_usdt.ref_compra or cot_usdt_compra)
                    else:
                        cot_applied = Decimal(cot_usd_compra);  ref_price = Decimal(cot_usd.ref_compra  or cot_usd_compra)
                    ok, err, mov_ars, monto_ccy = procesar_venta(request.user, moneda, monto, cot_applied, return_mov_ars=True)
                    if not ok: return HttpResponse(err, status=400)
                    registrar_spread_venta(
                        usuario=request.user, moneda_ccy=moneda, monto_ccy=monto,
                        ref_price=ref_price, applied_price=cot_applied,
                        movimiento=mov_ars, detalle_extra={"cot_id": (cot_usdt.id if moneda=='USDT' else cot_usd.id)}
                    )

                messages.success(request, "Operación realizada con éxito.")
                return redirect('dashboard')

            except Exception:
                return HttpResponse("Ocurrió un error al procesar la operación.", status=400)

        elif operacion == 'swap':
            direction = request.POST.get('swap_direccion')  # 'USD_to_USDT' | 'USDT_to_USD'
            try:
                amount = Decimal(request.POST.get('monto'))
            except Exception:
                messages.error(request, "Monto inválido.")
                return redirect('dashboard')

            ok, err, mov_dest, fee_amount, fee_currency = procesar_swap(request.user, direction, amount, return_fee=True)
            if not ok:
                messages.error(request, err)
                return redirect('dashboard')

            # registrar fee cobrado
            registrar_comision_swap(
                usuario=request.user, direccion=direction,
                fee_amount=fee_amount, fee_currency=fee_currency,
                movimiento=mov_dest, detalle_extra={"swap_fee_bps": getattr(settings,'SWAP_FEE_BPS',100)}
            )

            messages.success(request, "Swap realizado con éxito.")
            return redirect('dashboard')

        else:
            return HttpResponse("Operación no válida", status=400)

    # GET
    return render(request, 'usuarios/operar.html', {
        'cot_usdt': {'compra': cot_usdt_compra, 'venta': cot_usdt_venta},
        'cot_usd':  {'compra': cot_usd_compra,  'venta': cot_usd_venta},
        'swap_fee_bps': getattr(settings, 'SWAP_FEE_BPS', 100),
        'swap_rate': Decimal('1.00'),
    })




def procesar_compra(usuario, moneda, monto_ars, cotizacion_venta, *, return_mov_ccy=False):
    from usuarios.services.boletos import emitir_boleto
    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)
        if monto_ars <= 0 or u.saldo_ars < monto_ars:
            return False, "Saldo ARS insuficiente o monto inválido", None

        recibido = q2(Decimal(monto_ars) / Decimal(cotizacion_venta))

        ars_antes = u.saldo_ars
        u.saldo_ars = q2(u.saldo_ars - monto_ars)

        if moneda == 'USDT':
            mon_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt + recibido)
            mon_despues = u.saldo_usdt
        else:
            mon_antes = u.saldo_usd
            u.saldo_usd = q2(u.saldo_usd + recibido)
            mon_despues = u.saldo_usd

        u.save()
        ars_despues = u.saldo_ars

        registrar_movimiento(
            usuario=u, tipo='compra', moneda='ARS', monto=-monto_ars,
            descripcion=f'Compra de {moneda} a ${cotizacion_venta}',
            saldo_antes=ars_antes, saldo_despues=ars_despues
        )
        mov_ccy = registrar_movimiento(
            usuario=u, tipo='compra', moneda=moneda, monto=recibido,
            descripcion=f'Compra de {moneda} con ${monto_ars} ARS',
            saldo_antes=mon_antes, saldo_despues=mon_despues
        )

        numero = gen_numero_boleto()
        snapshot = {
            'titulo': f'Compra de {moneda}',
            'estado': 'Completo',
            'monto_debitado_fmt': fmt_money(monto_ars, '$'),
            'comision_total_fmt': None,
            'monto_origen_fmt': fmt_money(monto_ars, '$'),
            'tasa_fmt': f"{q2(cotizacion_venta)} ARS / {moneda}",
            'monto_destino_fmt': fmt_ccy(recibido, moneda),
            'cliente': cliente_ctx(u),
            'psav': {
                'nombre': settings.EMPRESA_NOMBRE,
                'cuit': settings.EMPRESA_CUIT,
                'domicilio': settings.EMPRESA_DOMICILIO,
                'contacto': settings.SUPPORT_CONTACTO,
                'leyenda_psav': settings.PSAV_LEYENDA or '',
            },
        }
        tipo_bol = 'compra_ars_usdt' if moneda == 'USDT' else 'compra_ars_usd'
        emitir_boleto(u, tipo_bol, numero, snapshot, movimiento=mov_ccy)

        if return_mov_ccy:
            return True, None, mov_ccy
        return True, None, None


def procesar_venta(usuario, moneda, monto_moneda, cotizacion_compra, *, return_mov_ars=False):
    from usuarios.services.boletos import emitir_boleto
    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)

        if monto_moneda <= 0:
            return False, "Monto inválido", None, None

        if moneda == 'USDT':
            if u.saldo_usdt < monto_moneda:
                return False, "Saldo USDT insuficiente", None, None
            mon_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt - monto_moneda)
            mon_despues = u.saldo_usdt
        else:
            if u.saldo_usd < monto_moneda:
                return False, "Saldo USD insuficiente", None, None
            mon_antes = u.saldo_usd
            u.saldo_usd = q2(u.saldo_usd - monto_moneda)
            mon_despues = u.saldo_usd

        ars_recibe = q2(Decimal(monto_moneda) * Decimal(cotizacion_compra))
        ars_antes = u.saldo_ars
        u.saldo_ars = q2(u.saldo_ars + ars_recibe)
        u.save()
        ars_despues = u.saldo_ars

        registrar_movimiento(
            usuario=u, tipo='venta', moneda=moneda, monto=-monto_moneda,
            descripcion=f'Venta de {moneda} a ${cotizacion_compra}',
            saldo_antes=mon_antes, saldo_despues=mon_despues
        )
        mov_ars = registrar_movimiento(
            usuario=u, tipo='venta', moneda='ARS', monto=ars_recibe,
            descripcion=f'Venta de {moneda}. ARS acreditado.',
            saldo_antes=ars_antes, saldo_despues=ars_despues
        )

        numero = gen_numero_boleto()
        snapshot = {
            'titulo': f'Venta de {moneda}',
            'estado': 'Completo',
            'monto_debitado_fmt': fmt_ccy(monto_moneda, moneda),
            'comision_total_fmt': None,
            'monto_origen_fmt': fmt_ccy(monto_moneda, moneda),
            'tasa_fmt': f"{q2(cotizacion_compra)} ARS / {moneda}",
            'monto_destino_fmt': fmt_money(ars_recibe, '$'),
            'cliente': cliente_ctx(u),
            'psav': {
                'nombre': settings.EMPRESA_NOMBRE,
                'cuit': settings.EMPRESA_CUIT,
                'domicilio': settings.EMPRESA_DOMICILIO,
                'contacto': settings.SUPPORT_CONTACTO,
                'leyenda_psav': settings.PSAV_LEYENDA or '',
            },
        }
        tipo_bol = 'venta_usdt_ars' if moneda == 'USDT' else 'venta_usd_ars'
        emitir_boleto(u, tipo_bol, numero, snapshot, movimiento=mov_ars)

        if return_mov_ars:
            return True, None, mov_ars, monto_moneda
        return True, None, None, None


def procesar_swap(usuario, direccion: str, amount: Decimal, *, rate=Decimal('1.00'), fee_bps=None, return_fee=False):
    from usuarios.services.boletos import emitir_boleto
    fee_bps = Decimal(fee_bps if fee_bps is not None else getattr(settings, 'SWAP_FEE_BPS', 100))
    fee_factor = (Decimal('1') - (fee_bps / Decimal('10000')))

    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)
        if amount <= 0:
            return False, "El monto debe ser mayor a 0.", None, None, None

        if direccion == 'USD_to_USDT':
            if u.saldo_usd < amount:
                return False, "Saldo USD insuficiente", None, None, None

            usd_antes = u.saldo_usd
            u.saldo_usd = q2(u.saldo_usd - amount)
            usd_despues = u.saldo_usd

            usdt_bruto = amount * rate
            usdt_neto = q2(usdt_bruto * fee_factor)
            fee_amt   = q2(usdt_bruto - usdt_neto)  # fee en USDT

            usdt_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt + usdt_neto)
            usdt_despues = u.saldo_usdt
            u.save()

            registrar_movimiento(
                usuario=u, tipo='venta', moneda='USD', monto=amount,
                descripcion=f'Swap USD→USDT al rate {rate}, fee {fee_bps} bps',
                saldo_antes=usd_antes, saldo_despues=usd_despues
            )
            mov_dest = registrar_movimiento(
                usuario=u, tipo='compra', moneda='USDT', monto=usdt_neto,
                descripcion=f'Swap USD→USDT al rate {rate}, fee {fee_bps} bps',
                saldo_antes=usdt_antes, saldo_despues=usdt_despues
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': 'Swap USD → USDT',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_ccy(amount, 'USD'),
                'comision_total_fmt': f"{fee_amt} USDT" if fee_amt > 0 else None,
                'monto_origen_fmt': fmt_ccy(amount, 'USD'),
                'tasa_fmt': f"{rate} USDT / USD — fee {fee_bps} bps",
                'monto_destino_fmt': fmt_ccy(usdt_neto, 'USDT'),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            emitir_boleto(u, 'swap_usd_usdt', numero, snapshot, movimiento=mov_dest)
            if return_fee:
                return True, None, mov_dest, fee_amt, 'USDT'
            return True, None, None, None, None

        elif direccion == 'USDT_to_USD':
            if u.saldo_usdt < amount:
                return False, "Saldo USDT insuficiente", None, None, None

            usdt_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt - amount)
            usdt_despues = u.saldo_usdt

            usd_bruto = amount / rate
            usd_neto = q2(usd_bruto * fee_factor)
            fee_amt  = q2(usd_bruto - usd_neto)  # fee en USD

            usd_antes = u.saldo_usd
            u.saldo_usd = q2(u.saldo_usd + usd_neto)
            usd_despues = u.saldo_usd
            u.save()

            registrar_movimiento(
                usuario=u, tipo='venta', moneda='USDT', monto=amount,
                descripcion=f'Swap USDT→USD al rate {rate}, fee {fee_bps} bps',
                saldo_antes=usdt_antes, saldo_despues=usdt_despues
            )
            mov_dest = registrar_movimiento(
                usuario=u, tipo='compra', moneda='USD', monto=usd_neto,
                descripcion=f'Swap USDT→USD al rate {rate}, fee {fee_bps} bps',
                saldo_antes=usd_antes, saldo_despues=usd_despues
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': 'Swap USDT → USD',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_ccy(amount, 'USDT'),
                'comision_total_fmt': f"{fee_amt} USD" if fee_amt > 0 else None,
                'monto_origen_fmt': fmt_ccy(amount, 'USDT'),
                'tasa_fmt': f"{rate} USDT / USD — fee {fee_bps} bps",
                'monto_destino_fmt': fmt_ccy(usd_neto, 'USD'),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            emitir_boleto(u, 'swap_usdt_usd', numero, snapshot, movimiento=mov_dest)
            if return_fee:
                return True, None, mov_dest, fee_amt, 'USD'
            return True, None, None, None, None

        return False, "Dirección de swap inválida", None, None, None

@login_required
def solicitar_retiro(request):
    if request.method == 'POST':
        alias = request.POST.get('alias')
        cbu = request.POST.get('cbu')
        banco = request.POST.get('banco')
        monto = Decimal(request.POST.get('monto'))

        if monto <= 0 or request.user.saldo_ars < monto:
            return HttpResponse("Saldo insuficiente o monto inválido", status=400)

        # Registrar solicitud
        RetiroARS.objects.create(
            usuario=request.user,
            alias=alias,
            cbu=cbu,
            banco=banco,
            monto=monto
        )

        # Registrar movimiento: descontar saldo
        saldo_antes = request.user.saldo_ars
        request.user.saldo_ars -= monto
        request.user.save()
        saldo_despues = request.user.saldo_ars

        registrar_movimiento(
            usuario=request.user,
            tipo='retiro',
            moneda='ARS',
            monto=-monto,
            descripcion=f'Solicitud de retiro ARS ({alias})',
            saldo_antes=saldo_antes,
            saldo_despues=saldo_despues
        )

        return redirect('dashboard')

    return render(request, 'usuarios/solicitar_retiro.html')

@login_required
def historial_retiros(request):
    retiros = RetiroARS.objects.filter(usuario=request.user).order_by('-fecha_solicitud')
    return render(request, 'historial_retiros.html', {'retiros': retiros})

@login_required
@user_passes_test(es_admin)
def aprobar_retiro(request, id):
    retiro = get_object_or_404(RetiroARS, id=id)
    if request.method == 'POST' and retiro.estado == 'pendiente':
        retiro.estado = 'aprobado'
        retiro.save()
    return HttpResponseRedirect(reverse('panel_admin'))

@login_required
@user_passes_test(es_admin)
def enviar_retiro(request, id):
    from usuarios.services.boletos import emitir_boleto
    retiro = get_object_or_404(RetiroARS, id=id)
    if request.method == 'POST' and retiro.estado == 'aprobado':
        with transaction.atomic():
            retiro.estado = 'enviado'
            retiro.save()

            u = retiro.usuario
            saldo_actual = u.saldo_ars

            mov = registrar_movimiento(
                usuario=u,
                tipo='retiro', moneda='ARS', monto=retiro.monto,
                descripcion=f'Retiro de ${retiro.monto} ARS enviado por admin a {retiro.alias} / {retiro.cbu or "s/CBU"} ({retiro.banco or "s/banco"})',
                saldo_antes=saldo_actual, saldo_despues=saldo_actual
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': 'Retiro ARS enviado',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_money(retiro.monto, '$'),
                'comision_total_fmt': None,
                'monto_origen_fmt': fmt_money(retiro.monto, '$'),
                'tasa_fmt': f"Alias/CBU: {retiro.alias} / {retiro.cbu or '—'}",
                'monto_destino_fmt': fmt_money(retiro.monto, '$'),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            emitir_boleto(u, 'retiro_ars', numero, snapshot, movimiento=mov)

        crear_notificacion(u, f"Tu retiro de ${retiro.monto} ARS fue enviado.")
    return HttpResponseRedirect(reverse('panel_admin'))






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
def obtener_notificaciones(request):
    queryset = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')
    notificaciones = list(queryset[:10])  # slicing primero

    # marcar como leídas solo esas
    Notificacion.objects.filter(id__in=[n.id for n in notificaciones]).update(leida=True)

    data = [
        {
            'mensaje': n.mensaje,
            'fecha': n.fecha.strftime('%d/%m/%Y %H:%M'),
        }
        for n in notificaciones
    ]
    return JsonResponse({'notificaciones': data})


@login_required
def contar_notificaciones(request):
    cantidad = Notificacion.objects.filter(usuario=request.user, leida=False).count()
    return JsonResponse({'no_leidas': cantidad})

@login_required
def solicitar_retiro_cripto(request):
    if request.method == 'POST':
        moneda = request.POST.get('moneda')
        monto = Decimal(request.POST.get('monto', '0'))
        wallet = request.POST.get('direccion_wallet')

        saldo = getattr(request.user, f'saldo_{moneda.lower()}')

        if monto > saldo:
            messages.error(request, "No tenés saldo suficiente.")
            return redirect('dashboard')

        RetiroCrypto.objects.create(
            usuario=request.user,
            moneda=moneda,
            monto=monto,
            direccion_wallet=wallet,
            estado='pendiente'
        )

        setattr(request.user, f'saldo_{moneda.lower()}', saldo - monto)
        request.user.save()

        registrar_movimiento(
            usuario=request.user,
            tipo='retiro',
            moneda=moneda,
            monto=monto,
            descripcion=f'Solicitud de retiro {moneda} - pendiente',
            saldo_antes=saldo,
            saldo_despues=saldo - monto
        )

        crear_notificacion(request.user, f"Tu retiro de {monto} {moneda} está pendiente de aprobación.")
        messages.success(request, f"Solicitud de retiro enviada: {monto} {moneda}")
        return redirect('dashboard')


@login_required
@user_passes_test(es_admin)
def aprobar_retiro_cripto(request, id):
    from usuarios.services.boletos import emitir_boleto
    retiro = get_object_or_404(RetiroCrypto, id=id)

    if request.method == 'POST' and retiro.estado == 'pendiente':
        red  = request.POST.get('red', '')
        txid = request.POST.get('txid', '')

        with transaction.atomic():
            retiro.estado = 'enviado'
            retiro.admin_responsable = request.user
            retiro.save()

            u = retiro.usuario
            saldo_actual = getattr(u, f'saldo_{retiro.moneda.lower()}')

            mov = registrar_movimiento(
                usuario=u, tipo='retiro', moneda=retiro.moneda, monto=retiro.monto,
                descripcion=f'Retiro {retiro.moneda} aprobado por admin a {retiro.direccion_wallet}',
                saldo_antes=saldo_actual, saldo_despues=saldo_actual
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': f'Retiro de {retiro.moneda}',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_ccy(retiro.monto, retiro.moneda),
                'comision_total_fmt': None,
                'monto_origen_fmt': fmt_ccy(retiro.monto, retiro.moneda),
                'tasa_fmt': f"Red: {red}" if red else '—',
                'monto_destino_fmt': fmt_ccy(retiro.monto, retiro.moneda),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            onchain = {
                'red': red,
                'origen': '(custodia interna)',
                'destino': retiro.direccion_wallet,
                'txid': txid,
                'fecha_hora': localtime().strftime("%d/%m/%Y %H:%M:%S"),
            }
            emitir_boleto(u, f"retiro_{retiro.moneda.lower()}", numero, snapshot, movimiento=mov, onchain=onchain)

        crear_notificacion(u, f"Tu retiro de {retiro.monto} {retiro.moneda} fue enviado.")
        messages.success(request, "Retiro aprobado y enviado.")

    return redirect('panel_admin')


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
def aprobar_deposito_usdt(request, deposito_id):
    from usuarios.services.boletos import emitir_boleto
    dep = get_object_or_404(DepositoUSDT, id=deposito_id)  # asegurate de importar el modelo
    if request.method == 'POST' and dep.estado == 'pendiente':
        with transaction.atomic():
            u = Usuario.objects.select_for_update().get(pk=dep.usuario_id)

            antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt + dep.monto)
            u.save()
            despues = u.saldo_usdt

            dep.estado = 'aprobado'
            dep.save()

            mov = registrar_movimiento(
                usuario=u, tipo='deposito', moneda='USDT', monto=dep.monto,
                descripcion=f'Depósito USDT aprobado por admin (red: {dep.red}, txid: {dep.txid})',
                admin=request.user, saldo_antes=antes, saldo_despues=despues
            )

            numero = gen_numero_boleto()
            snapshot = {
                'titulo': 'Acreditación de depósito USDT',
                'estado': 'Completo',
                'monto_debitado_fmt': fmt_ccy(dep.monto, 'USDT'),
                'comision_total_fmt': None,
                'monto_origen_fmt': fmt_ccy(dep.monto, 'USDT'),
                'tasa_fmt': f"Red: {dep.red}" if getattr(dep, 'red', '') else '—',
                'monto_destino_fmt': fmt_ccy(dep.monto, 'USDT'),
                'cliente': cliente_ctx(u),
                'psav': {
                    'nombre': settings.EMPRESA_NOMBRE,
                    'cuit': settings.EMPRESA_CUIT,
                    'domicilio': settings.EMPRESA_DOMICILIO,
                    'contacto': settings.SUPPORT_CONTACTO,
                    'leyenda_psav': settings.PSAV_LEYENDA or '',
                },
            }
            onchain = {
                'red': dep.red,
                'origen': getattr(dep, 'wallet_origen', ''),
                'destino': getattr(dep, 'wallet_destino', getattr(settings, 'WALLET_EMPRESA', '')),
                'txid': dep.txid,
                'fecha_hora': localtime().strftime('%d/%m/%Y %H:%M:%S'),
            }
            emitir_boleto(u, 'deposito_usdt', numero, snapshot, movimiento=mov, onchain=onchain)

        crear_notificacion(u, f"Tu depósito de {dep.monto} USDT fue aprobado.")
    return redirect('panel_depositos_usdt')


@login_required
@user_passes_test(es_admin)
def rechazar_deposito_usdt(request, deposito_id):
    dep = get_object_or_404(DepositoUSDT, id=deposito_id)
    if dep.estado == 'pendiente':
        dep.estado = 'rechazado'
        dep.save()

        registrar_movimiento(
            usuario=dep.usuario,
            tipo='ajuste',
            moneda='USDT',
            monto=0,
            descripcion=f'Depósito USDT rechazado por admin. Monto solicitado: {dep.monto} USDT (txid: {dep.txid})'
        )
        crear_notificacion(dep.usuario, f"Tu depósito de {dep.monto} USDT fue rechazado.")
    return redirect('panel_depositos_usdt')

from decimal import Decimal

# @login_required
# def swap_usd_usdt(request):
#     if request.user.estado_verificacion != 'aprobado':
#         return render(request, 'usuarios/no_verificado.html')

#     if request.method == 'POST':
#         # direction: 'USD_to_USDT' o 'USDT_to_USD'
#         direction = request.POST.get('direction')
#         try:
#             amount = Decimal(request.POST.get('amount', '0'))
#         except:
#             messages.error(request, "Monto inválido.")
#             return redirect('dashboard')

#         if amount <= 0:
#             messages.error(request, "El monto debe ser mayor a 0.")
#             return redirect('dashboard')

#         fee_bps = getattr(settings, 'SWAP_FEE_BPS', 100)  # 1%
#         fee_factor = Decimal('1') - (Decimal(str(fee_bps)) / Decimal('10000'))

#         # Tasa base (paridad). Si luego querés, traé esto de tu tabla Cotizacion (USDT/USD).
#         rate = Decimal('1.00')

#         with transaction.atomic():
#             usuario = Usuario.objects.select_for_update().get(pk=request.user.pk)

#             if direction == 'USD_to_USDT':
#                 if usuario.saldo_usd < amount:
#                     messages.error(request, "Saldo USD insuficiente.")
#                     return redirect('dashboard')

#                 # venta de USD, compra de USDT (neto con fee)
#                 usd_antes = usuario.saldo_usd
#                 usuario.saldo_usd = (usuario.saldo_usd - amount)
#                 usd_despues = usuario.saldo_usd

#                 usdt_bruto = amount * rate  # ~ igual
#                 usdt_neto = (usdt_bruto * fee_factor)  # fee aplicado

#                 usdt_antes = usuario.saldo_usdt
#                 usuario.saldo_usdt = (usuario.saldo_usdt + usdt_neto)
#                 usdt_despues = usuario.saldo_usdt

#                 usuario.save()

#                 # movimientos: venta USD, compra USDT
#                 registrar_movimiento(
#                     usuario=usuario,
#                     tipo='venta',
#                     moneda='USD',
#                     monto=amount,  # vendiste X USD
#                     descripcion=f'Swap USD→USDT al rate {rate}, fee {fee_bps} bps',
#                     saldo_antes=usd_antes,
#                     saldo_despues=usd_despues
#                 )
#                 registrar_movimiento(
#                     usuario=usuario,
#                     tipo='compra',
#                     moneda='USDT',
#                     monto=usdt_neto,  # acreditado neto
#                     descripcion=f'Swap USD→USDT al rate {rate}, fee {fee_bps} bps',
#                     saldo_antes=usdt_antes,
#                     saldo_despues=usdt_despues
#                 )

#                 crear_notificacion(usuario, f"Swap USD→USDT exitoso: {amount} USD → {usdt_neto} USDT (fee {fee_bps} bps).")
#                 messages.success(request, f"Swap USD→USDT realizado. Acreditado: {usdt_neto} USDT.")

#             elif direction == 'USDT_to_USD':
#                 if usuario.saldo_usdt < amount:
#                     messages.error(request, "Saldo USDT insuficiente.")
#                     return redirect('dashboard')

#                 # venta de USDT, compra de USD (neto con fee)
#                 usdt_antes = usuario.saldo_usdt
#                 usuario.saldo_usdt = (usuario.saldo_usdt - amount)
#                 usdt_despues = usuario.saldo_usdt

#                 usd_bruto = amount / rate  # ~ igual
#                 usd_neto = (usd_bruto * fee_factor)

#                 usd_antes = usuario.saldo_usd
#                 usuario.saldo_usd = (usuario.saldo_usd + usd_neto)
#                 usd_despues = usuario.saldo_usd

#                 usuario.save()

#                 registrar_movimiento(
#                     usuario=usuario,
#                     tipo='venta',
#                     moneda='USDT',
#                     monto=amount,
#                     descripcion=f'Swap USDT→USD al rate {rate}, fee {fee_bps} bps',
#                     saldo_antes=usdt_antes,
#                     saldo_despues=usdt_despues
#                 )
#                 registrar_movimiento(
#                     usuario=usuario,
#                     tipo='compra',
#                     moneda='USD',
#                     monto=usd_neto,
#                     descripcion=f'Swap USDT→USD al rate {rate}, fee {fee_bps} bps',
#                     saldo_antes=usd_antes,
#                     saldo_despues=usd_despues
#                 )

#                 crear_notificacion(usuario, f"Swap USDT→USD exitoso: {amount} USDT → {usd_neto} USD (fee {fee_bps} bps).")
#                 messages.success(request, f"Swap USDT→USD realizado. Acreditado: {usd_neto} USD.")
#             else:
#                 messages.error(request, "Dirección de swap inválida.")
#                 return redirect('dashboard')

#         return redirect('dashboard')

#     # GET: simple form
#     return render(request, 'usuarios/swap.html', {
#         'fee_bps': getattr(settings, 'SWAP_FEE_BPS', 100),
#         'rate': Decimal('1.00'),
#     })


@login_required
@user_passes_test(es_admin)
def rechazar_retiro_ars(request, id):
    retiro = get_object_or_404(RetiroARS, id=id)
    if request.method == 'POST' and retiro.estado in ('pendiente', 'aprobado'):
        with transaction.atomic():
            u = Usuario.objects.select_for_update().get(pk=retiro.usuario_id)
            saldo_antes = u.saldo_ars
            u.saldo_ars = q2(u.saldo_ars + retiro.monto)
            u.save()
            retiro.estado = 'rechazado'
            retiro.save()
            registrar_movimiento(
                usuario=u, tipo='ajuste', moneda='ARS', monto=retiro.monto,
                descripcion=f'Retiro ARS rechazado. Se recredita ${retiro.monto}.',
                saldo_antes=saldo_antes, saldo_despues=u.saldo_ars
            )
            crear_notificacion(u, f"Tu retiro de ${retiro.monto} ARS fue rechazado. Se recreditó el saldo.")
    return redirect('panel_retiros')

@login_required
@user_passes_test(es_admin)
def rechazar_retiro_cripto(request, id):
    retiro = get_object_or_404(RetiroCrypto, id=id)
    if request.method == 'POST' and retiro.estado == 'pendiente':
        with transaction.atomic():
            u = Usuario.objects.select_for_update().get(pk=retiro.usuario_id)
            campo = 'saldo_usdt' if retiro.moneda == 'USDT' else 'saldo_usd'
            antes = getattr(u, campo)
            setattr(u, campo, q2(antes + retiro.monto))
            u.save()
            retiro.estado = 'rechazado'
            retiro.save()
            registrar_movimiento(
                usuario=u, tipo='ajuste', moneda=retiro.moneda, monto=retiro.monto,
                descripcion=f'Retiro {retiro.moneda} rechazado. Se recredita {retiro.monto}.',
                saldo_antes=antes, saldo_despues=getattr(u, campo)
            )
            crear_notificacion(u, f"Tu retiro de {retiro.monto} {retiro.moneda} fue rechazado y se recreditó.")
    return redirect('panel_retiros')





def verificar_boleto(request, numero):
    b = get_object_or_404(BoletoOperacion, numero=numero)
    ctx = {
        "numero": b.numero,
        "fecha": b.fecha_emision,
        "usuario": b.usuario.username,
        "tipo": b.get_tipo_display(),
        "pdf_sha256": b.pdf_sha256,
        "anulado": b.anulado,
    }
    return render(request, "boletos/verificar.html", ctx)

@login_required
def descargar_boleto(request, numero):
    qs = BoletoOperacion.objects.all()
    if not request.user.is_staff:
        qs = qs.filter(usuario=request.user)
    b = get_object_or_404(qs, numero=numero)
    return FileResponse(b.pdf.open("rb"), as_attachment=True, filename=f"{b.numero}.pdf")

@login_required
def comprobantes(request):
    boletas = BoletoOperacion.objects.filter(usuario=request.user).order_by('-fecha_emision')
    return render(request, "usuarios/comprobantes.html", {"boletas": boletas})




#NUEVAS ACTUALIZACIONES

@login_required
def perfil(request):
    """Landing de Perfil (lectura)."""
    return render(request, 'usuarios/perfil.html')

@login_required
def configuracion(request):
    """Configuración con tabs (Perfil / Seguridad / Apariencia / Ayuda)."""
    return render(request, 'usuarios/configuracion.html')

def faq(request):
    # Podés mover esto a BD si querés
    faqs = [
        {"q":"¿Cómo compro USDT con ARS?",
         "a":"Ir a Operar → Comprar, elegí USDT, ingresá el monto en ARS y confirmá el resumen."},
        {"q":"¿Cuál es la comisión del swap USD ⇄ USDT?",
         "a":"Mostramos la tasa base y los bps de comisión en el formulario. El preview ya calcula el monto neto."},
        {"q":"¿Dónde veo mis movimientos?",
         "a":"En Dashboard aparecen los últimos 10. Para ver y exportar: Movimientos (en el menú)."},
        {"q":"¿Cómo verifico mi identidad (KYC)?",
         "a":"Durante el registro, en el paso KYC, subí frente y dorso de tu DNI con buena iluminación."},
        {"q":"¿Puedo retirar USD en sucursal?",
         "a":"Sí. Comprá USD en la app y acercate a una sucursal habilitada para retiro en efectivo."},
    ]
    return render(request, "usuarios/faq.html", {"faqs": faqs})

@login_required
def soporte(request):
    if request.method == "POST":
        form = SupportTicketForm(request.POST, request.FILES)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.usuario = request.user
            ticket.save()
            try:
                send_mail(
                    subject=f"[Full Finanzas] Nuevo ticket #{ticket.id}: {ticket.asunto}",
                    message=(
                        f"Usuario: {request.user.username} ({ticket.email})\n"
                        f"Categoría: {ticket.categoria}\n"
                        f"Prioridad: {ticket.prioridad}\n"
                        f"Mensaje:\n{ticket.mensaje}\n"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=getattr(settings, "SOPORTE_TO", []),
                    fail_silently=True,
                )
            except Exception:
                pass  # no interrumpimos UX si el mail falla

            # Confirmación al usuario (opcional)
            try:
                send_mail(
                    subject=f"Recibimos tu ticket #{ticket.id}",
                    message=(
                        f"Hola {request.user.username},\n\n"
                        f"Recibimos tu solicitud y ya la estamos revisando.\n"
                        f"Asunto: {ticket.asunto}\n"
                        f"ID: #{ticket.id}\n\n"
                        f"Te responderemos a este email.\n\n"
                        f"— Equipo de Soporte Full Finanzas"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[ticket.email],
                    fail_silently=True,
                )
            except Exception:
                pass
            messages.success(request, "Tu ticket fue creado correctamente. ¡Te vamos a escribir por email!")
            return redirect("soporte")
        else:
            messages.error(request, "Revisá los campos resaltados.")
    else:
        form = SupportTicketForm(initial={"email": request.user.email})
    return render(request, "usuarios/soporte.html", {"form": form})


@login_required
def mis_tickets(request):
    qs = SupportTicket.objects.filter(usuario=request.user).order_by("-creado_en")
    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "usuarios/mis_tickets.html", {"page_obj": page_obj})

# -----------------------
# Acciones: Perfil
# -----------------------

@require_POST
@login_required
def actualizar_perfil(request):
    """
    Actualiza datos de perfil PERMITIDOS (NO identidad).
    Inmutables: first_name, last_name, doc_tipo, doc_nro
    Permitidos: telefono, domicilio (legacy), y domicilio estructurado,
                nacionalidad, fecha_nacimiento, lugar_nacimiento,
                persona_tipo, estado_civil, sexo, código postal, etc.
    """

    u = request.user

    # --- Detectar intento de cambiar identidad (se ignora) ---
    tried_identity = False
    if "first_name" in request.POST and (request.POST.get("first_name","").strip() != u.first_name):
        tried_identity = True
    if "last_name" in request.POST and (request.POST.get("last_name","").strip() != u.last_name):
        tried_identity = True
    if "doc_tipo" in request.POST and (request.POST.get("doc_tipo","").strip().upper() != (u.doc_tipo or "")):
        tried_identity = True
    if "doc_nro" in request.POST and (request.POST.get("doc_nro","").strip() != (u.doc_nro or "")):
        tried_identity = True
    # Campo combinado "doc" (tipo + nro) también se ignora:
    if "doc" in request.POST and request.POST.get("doc","").strip():
        tried_identity = True

    # --- Campos permitidos ---
    telefono       = (request.POST.get('telefono')       or '').strip()
    domicilio      = (request.POST.get('domicilio')      or '').strip()  # legacy (opcional)
    nacionalidad   = (request.POST.get('nacionalidad')   or '').strip()
    lugar_nac      = (request.POST.get('lugar_nacimiento') or '').strip()
    persona_tipo   = (request.POST.get('persona_tipo')   or '').strip()  # "FISICA"/"JURIDICA"
    estado_civil   = (request.POST.get('estado_civil')   or '').strip()
    sexo           = (request.POST.get('sexo')           or '').strip()

    # Fecha (YYYY-MM-DD)
    fecha_nac = None
    raw_fecha = (request.POST.get('fecha_nacimiento') or '').strip()
    if raw_fecha:
        try:
            fecha_nac = datetime.strptime(raw_fecha, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Fecha de nacimiento inválida. Usá formato AAAA-MM-DD.")
            return redirect(f"{reverse('configuracion')}?tab=perfil")

    # Domicilio estructurado
    pais_id        = request.POST.get('pais') or None
    provincia_id   = request.POST.get('provincia') or None
    localidad_id   = request.POST.get('localidad') or None
    codigo_postal  = (request.POST.get('codigo_postal') or '').strip()
    calle          = (request.POST.get('calle') or '').strip()
    numero_calle   = (request.POST.get('numero_calle') or '').strip()
    piso           = (request.POST.get('piso') or '').strip()
    depto          = (request.POST.get('depto') or '').strip()

    # Resolver FKs (opcionales)
    pais_obj = prov_obj = loc_obj = None
    try:
        if pais_id:
            pais_obj = Pais.objects.get(pk=pais_id)
        if provincia_id:
            prov_obj = Provincia.objects.get(pk=provincia_id)
        if localidad_id:
            loc_obj = Localidad.objects.get(pk=localidad_id)
    except (Pais.DoesNotExist, Provincia.DoesNotExist, Localidad.DoesNotExist):
        messages.error(request, "Selección de país/provincia/localidad inválida.")
        return redirect(f"{reverse('configuracion')}?tab=perfil")

    try:
        with transaction.atomic():
            # Asignar SOLO campos permitidos
            u.telefono         = telefono
            u.domicilio        = domicilio  # si todavía lo usás en algún lado
            u.nacionalidad     = nacionalidad
            u.lugar_nacimiento = lugar_nac
            u.persona_tipo     = persona_tipo or u.persona_tipo
            u.estado_civil     = estado_civil or u.estado_civil
            u.sexo             = sexo or u.sexo

            u.fecha_nacimiento = fecha_nac if raw_fecha else u.fecha_nacimiento

            u.pais        = pais_obj
            u.provincia   = prov_obj
            u.localidad   = loc_obj
            u.codigo_postal = codigo_postal
            u.calle         = calle
            u.numero_calle  = numero_calle
            u.piso          = piso
            u.depto         = depto

            # ¡NO tocar identidad!
            # u.first_name, u.last_name, u.doc_tipo, u.doc_nro -> se dejan igual

            u.save()

        if tried_identity:
            messages.info(request, "Nombre/Apellido y Documento no se pueden modificar; se mantuvieron sin cambios.")
        messages.success(request, "Perfil actualizado.")
    except Exception as e:
        messages.error(request, f"Ocurrió un error al actualizar el perfil: {e}")

    return redirect(f"{reverse('configuracion')}?tab=perfil")


# -----------------------
# Acciones: Seguridad
# -----------------------

@require_POST
@login_required
def cambiar_password(request):
    """
    Cambia la contraseña del usuario.
    Campos: actual, nueva, confirmar
    """
    u = request.user
    actual    = request.POST.get('actual', '')
    nueva     = request.POST.get('nueva', '')
    confirmar = request.POST.get('confirmar', '')

    if not u.check_password(actual):
        messages.error(request, "La contraseña actual no es correcta.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    if nueva != confirmar:
        messages.error(request, "La nueva contraseña y su confirmación no coinciden.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    if len(nueva) < 8:
        messages.error(request, "La nueva contraseña debe tener al menos 8 caracteres.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    try:
        u.set_password(nueva)
        u.save()
        # Mantener la sesión iniciada tras el cambio
        update_session_auth_hash(request, u)
        messages.success(request, "Contraseña actualizada correctamente.")
    except Exception as e:
        messages.error(request, f"No se pudo actualizar la contraseña: {e}")

    return redirect(f"{reverse('configuracion')}?tab=seguridad")


@require_POST
@login_required
def cambiar_email(request):
    """
    Cambia el email del usuario (pide contraseña actual).
    Campos: email, password
    """
    u = request.user
    email    = (request.POST.get('email') or '').strip().lower()
    password = request.POST.get('password') or ''

    if not u.check_password(password):
        messages.error(request, "La contraseña ingresada es incorrecta.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Ingresá un email válido.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    # Evitar duplicados de email en el sistema
    if User.objects.filter(email__iexact=email).exclude(pk=u.pk).exists():
        messages.error(request, "Ese email ya está en uso por otro usuario.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    try:
        u.email = email
        u.save()
        messages.success(request, "Email actualizado correctamente.")
    except Exception as e:
        messages.error(request, f"No se pudo actualizar el email: {e}")

    return redirect(f"{reverse('configuracion')}?tab=seguridad")


@login_required
def activar_2fa(request):
    """
    Activa 2FA (placeholder).
    NOTA: Esto solo marca el flag. Para 2FA real integrá django-otp/pyotp
    con enrolment (QR) + verificación del código TOTP.
    """
    u = request.user
    if getattr(u, 'has_2fa', False):
        messages.info(request, "2FA ya estaba activo.")
    else:
        try:
            u.has_2fa = True
            u.save()
            messages.success(request, "2FA activado. (Recuerda integrar TOTP para producción).")
        except Exception as e:
            messages.error(request, f"No se pudo activar 2FA: {e}")
    return redirect(f"{reverse('configuracion')}?tab=seguridad")


@login_required
def desactivar_2fa(request):
    """Desactiva 2FA (placeholder)."""
    u = request.user
    try:
        u.has_2fa = False
        u.save()
        messages.success(request, "2FA desactivado.")
    except Exception as e:
        messages.error(request, f"No se pudo desactivar 2FA: {e}")
    return redirect(f"{reverse('configuracion')}?tab=seguridad")


def tyc(request):
    if request.method == "POST":
        request.user.marcar_tyc_aceptado(getattr(settings,"TYC_VERSION",""))
        messages.success(request, "Términos aceptados.")
        next_url = request.POST.get("next") or "dashboard"
        return redirect(next_url)
    return render(request, "usuarios/tyc.html", {"version": getattr(settings,"TYC_VERSION","")})


def _filtrar_movimientos(request, base_qs):
    """
    Aplica filtros a partir de request.GET y devuelve el queryset filtrado
    + diccionario de valores para re-poblar el form.
    """
    params = {
        'desde': request.GET.get('desde', ''),
        'hasta': request.GET.get('hasta', ''),
        'tipo': request.GET.get('tipo', ''),          # ejemplo: 'compra', 'venta', 'swap', 'deposito', 'retiro'
        'moneda': request.GET.get('moneda', ''),      # 'ARS', 'USDT', 'USD'
        'q': request.GET.get('q', ''),                # texto libre en descripción/código
        'min': request.GET.get('min', ''),            # monto mínimo
        'max': request.GET.get('max', ''),            # monto máximo
        'orden': request.GET.get('orden', '-fecha'),  # orden por fecha default descendente
        'por_pagina': int(request.GET.get('por_pagina', '20') or 20),
    }

    qs = base_qs

    if params['desde']:
        d = parse_date(params['desde'])
        if d:
            qs = qs.filter(fecha__date__gte=d)

    if params['hasta']:
        h = parse_date(params['hasta'])
        if h:
            qs = qs.filter(fecha__date__lte=h)

    if params['tipo']:
        qs = qs.filter(tipo=params['tipo'])

    if params['moneda']:
        qs = qs.filter(moneda=params['moneda'])

    if params['q']:
        qs = qs.filter(
            models.Q(descripcion__icontains=params['q']) |
            models.Q(codigo__icontains=params['q'])
        )

    # Filtros por monto
    if params['min']:
        try:
            qs = qs.filter(monto__gte=Decimal(params['min']))
        except Exception:
            pass
    if params['max']:
        try:
            qs = qs.filter(monto__lte=Decimal(params['max']))
        except Exception:
            pass

    # Orden
    allowed_orders = {'fecha', '-fecha', 'monto', '-monto'}
    if params['orden'] not in allowed_orders:
        params['orden'] = '-fecha'
    qs = qs.order_by(params['orden'])

    return qs, params

@login_required
def mis_movimientos(request):
    base = Movimiento.objects.filter(usuario=request.user)
    qs, params = _filtrar_movimientos(request, base)

    totales = (
        qs.values('moneda').order_by().annotate(total=models.Sum('monto'))
    )

    paginator = Paginator(qs, params['por_pagina'])
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'usuarios/mis_movimientos.html', {
        'page_obj': page_obj,
        'params': params,
        'totales': totales,
        'por_pagina_opts': [20, 50, 100, 200],  # ← aquí
    })



@login_required
def exportar_movimientos(request):
    base = Movimiento.objects.filter(usuario=request.user)
    qs, params = _filtrar_movimientos(request, base)

    # CSV
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="movimientos.csv"'
    writer = csv.writer(response)
    writer.writerow(['Fecha', 'Tipo', 'Moneda', 'Monto', 'Saldo antes', 'Saldo después', 'Descripción', 'ID'])

    for m in qs.iterator():
        writer.writerow([
            m.fecha.isoformat(sep=' ', timespec='seconds'),
            m.get_tipo_display(),
            m.moneda,
            f"{m.monto:.2f}",
            f"{m.saldo_antes:.2f}",
            f"{m.saldo_despues:.2f}",
            m.descripcion or '',
            m.codigo or ''
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


# CONTABILIDAD 

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

def staff_required(u):
    return u.is_staff or u.is_superuser

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
def verify_email_notice(request):
    # Página que dice "revisá tu email" + botón "reenviar" + "cambiar email"
    if request.user.email_confirmed:
        return redirect('dashboard')  # ya confirmado
    return render(request, 'usuarios/verify_email_notice.html')

@login_required
@require_POST
def resend_verification(request):
    if request.user.email_confirmed:
        return redirect('dashboard')
    send_verification_email(request, request.user)
    request.user.email_confirm_sent_at = timezone.now()
    request.user.save(update_fields=['email_confirm_sent_at'])
    messages.success(request, "Te enviamos un nuevo email de verificación.")
    return redirect('verify_email_notice')

@login_required
def change_email_form(request):
    if request.user.email_confirmed:
        return redirect('dashboard')
    return render(request, 'usuarios/change_email_form.html')

@login_required
@require_POST
def change_email_submit(request):
    if request.user.email_confirmed:
        return redirect('dashboard')

    new_email = (request.POST.get('email') or '').strip().lower()
    if not new_email:
        messages.error(request, "Ingresá un email válido.")
        return redirect('change_email_form')

    # podés agregar validaciones extra (unicidad, etc.)
    if User.objects.filter(email=new_email).exclude(pk=request.user.pk).exists():
        messages.error(request, "Ese email ya está en uso.")
        return redirect('change_email_form')

    request.user.email = new_email
    request.user.email_confirmed = False
    request.user.save(update_fields=['email', 'email_confirmed'])

    send_verification_email(request, request.user)
    messages.success(request, "Actualizamos tu email. Te enviamos un nuevo enlace de verificación.")
    return redirect('verify_email_notice')

def verify_email(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid)
    except Exception:
        user = None

    if user and default_token_generator.check_token(user, token):
        if not user.email_confirmed:
            user.email_confirmed = True
            user.email_confirmed_at = timezone.now()
            user.is_active = True
            user.save(update_fields=['email_confirmed', 'email_confirmed_at'])
        messages.success(request, "¡Email verificado con éxito!")
        # Si está logueado va al dashboard; si no, al login
        if request.user.is_authenticated:
            return redirect('dashboard')
        return redirect('login')
    else:
        messages.error(request, "Enlace inválido o expirado. Pedí uno nuevo.")
        if request.user.is_authenticated:
            return redirect('verify_email_notice')
        return redirect('login')
    




