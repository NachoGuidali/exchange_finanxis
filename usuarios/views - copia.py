from django.shortcuts import render, redirect, get_object_or_404, HttpResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponseRedirect, FileResponse, HttpResponse
from .forms import RegistroUsuarioForm, DepositoARSForm, DepositoUSDTForm
from django.contrib import messages
from django.urls import reverse
from .models import Usuario, DepositoARS, Movimiento, Cotizacion, RetiroARS, Notificacion, RetiroCrypto, DepositoUSDT, BoletoOperacion, Provincia, Localidad, Pais
from decimal import Decimal, ROUND_DOWN
from django.views.decorators.http import require_GET, require_POST
from django.http import JsonResponse, Http404
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
import logging
import csv
from django.db.models import Q
from datetime import datetime
from django.utils.timezone import localtime
from .utils import registrar_movimiento, crear_notificacion, cliente_ctx
from django.db import transaction
from django.conf import settings
from django.contrib.auth import logout, get_user_model, update_session_auth_hash
from usuarios.services.boletos import emitir_boleto
import uuid
import hashlib


logger = logging.getLogger(__name__)

User = get_user_model()

# helpers de formato / numeración
def q2(x): return Decimal(x).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
def fmt_money(x, symbol='$'): return f"{symbol}{q2(Decimal(x)):,.2f}"
def fmt_ccy(x, ccy): return f"{q2(Decimal(x))} {ccy}"
def gen_numero_boleto(): return f"BOL-{localtime().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

def home(request):
    return render(request, "home.html")

#INICIO REGISTRO NUEVO

def registro(request):
    if request.method == 'POST':
        form = RegistroUsuarioForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Registro exitoso. Tu cuenta quedó en verificación.')
            return redirect('login')
    else:
        form = RegistroUsuarioForm()
    return render(request, 'usuarios/registro.html', {'form': form})


@require_GET
def geo_provincias(request):
    pais_id = request.GET.get("pais_id")
    if not pais_id:
        return JsonResponse({"results": []})
    qs = Provincia.objects.filter(pais_id=pais_id).order_by("nombre")
    data = [{"id": p.id, "nombre": p.nombre} for p in qs]
    return JsonResponse({"results": data})


@require_GET
def geo_localidades(request):
    prov_id = request.GET.get("provincia_id")
    if not prov_id:
        return JsonResponse({"results": []})
    qs = Localidad.objects.filter(provincia_id=prov_id).order_by("nombre")
    data = [{"id": l.id, "nombre": l.nombre} for l in qs]
    return JsonResponse({"results": data})

#fin registro nuevo


@login_required
@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada correctamente.")
    return redirect('login')

@login_required
def dashboard(request):
    if request.user.estado_verificacion != 'aprobado':
        return render(request, 'usuarios/no_verificado.html')
    
    movimientos = Movimiento.objects.filter(usuario=request.user).order_by('-fecha')
    cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
    cot_usd = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
    notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')[:10]
    notificaciones_no_leidas = Notificacion.objects.filter(usuario=request.user, leida=False)

    return render(request, 'usuarios/dashboard.html', {
        'movimientos': movimientos,
        'cot_usdt': cot_usdt,
        'cot_usd': cot_usd,
        'notificaciones': notificaciones,
        'notificaciones_no_leidas': notificaciones_no_leidas,
    })


def es_admin(user):
    return user.is_superuser or user.is_staff


@login_required
@user_passes_test(es_admin)
def panel_admin(request):
    usuarios = Usuario.objects.all().order_by('-date_joined')
    retiros = RetiroARS.objects.all().order_by('-fecha_solicitud')
    depositos = DepositoARS.objects.all().order_by('-fecha')
    movimientos = Movimiento.objects.all().order_by('-fecha')[:50]  # los últimos 50

    return render(request, 'usuarios/panel_admin.html', {
        'usuarios': usuarios,
        'retiros': retiros,
        'depositos': depositos,
        'movimientos': movimientos
    })

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

    cot_usdt_compra = cot_usdt.compra
    cot_usdt_venta  = cot_usdt.venta
    cot_usd_compra  = cot_usd.compra
    cot_usd_venta   = cot_usd.venta

    if request.method == 'POST':
        operacion = request.POST.get('operacion')

        # COMPRA / VENTA contra ARS
        if operacion in ('compra', 'venta'):
            moneda = request.POST.get('moneda')
            try:
                monto = Decimal(request.POST.get('monto'))
            except Exception:
                logger.error(f"[OPERAR ERROR] Usuario: {request.user.username} - Monto inválido")
                return HttpResponse("Monto inválido.", status=400)

            try:
                if operacion == 'compra':
                    cot = cot_usdt_venta if moneda == 'USDT' else cot_usd_venta
                    ok, err = procesar_compra(request.user, moneda, monto, cot)
                else:
                    cot = cot_usdt_compra if moneda == 'USDT' else cot_usd_compra
                    ok, err = procesar_venta(request.user, moneda, monto, cot)

                if not ok:
                    return HttpResponse(err, status=400)

                messages.success(request, "Operación realizada con éxito.")
                return redirect('dashboard')

            except Exception as e:
                logger.error(f"[OPERAR ERROR] Usuario: {request.user.username} - Error: {str(e)}")
                return HttpResponse("Ocurrió un error al procesar la operación.", status=400)

        # SWAP USD ⇄ USDT
        elif operacion == 'swap':
            direction = request.POST.get('swap_direccion')  # 'USD_to_USDT' | 'USDT_to_USD'
            try:
                amount = Decimal(request.POST.get('monto'))
            except Exception:
                messages.error(request, "Monto inválido.")
                return redirect('dashboard')

            ok, err = procesar_swap(request.user, direction, amount)
            if not ok:
                messages.error(request, err)
                return redirect('dashboard')

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




def procesar_compra(usuario, moneda, monto_ars, cotizacion_venta):
    from usuarios.services.boletos import emitir_boleto
    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)
        if monto_ars <= 0 or u.saldo_ars < monto_ars:
            return False, "Saldo ARS insuficiente o monto inválido"

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

        # boleto obligatorio
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

        return True, None


def procesar_venta(usuario, moneda, monto_moneda, cotizacion_compra):
    from usuarios.services.boletos import emitir_boleto
    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)

        if monto_moneda <= 0:
            return False, "Monto inválido"

        if moneda == 'USDT':
            if u.saldo_usdt < monto_moneda:
                return False, "Saldo USDT insuficiente"
            mon_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt - monto_moneda)
            mon_despues = u.saldo_usdt
        else:
            if u.saldo_usd < monto_moneda:
                return False, "Saldo USD insuficiente"
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

        return True, None

def procesar_swap(usuario, direccion: str, amount: Decimal, *, rate=Decimal('1.00'), fee_bps=None):
    from usuarios.services.boletos import emitir_boleto
    fee_bps = Decimal(fee_bps if fee_bps is not None else getattr(settings, 'SWAP_FEE_BPS', 100))
    fee_factor = (Decimal('1') - (fee_bps / Decimal('10000')))

    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)
        if amount <= 0:
            return False, "El monto debe ser mayor a 0."

        if direccion == 'USD_to_USDT':
            if u.saldo_usd < amount:
                return False, "Saldo USD insuficiente"

            usd_antes = u.saldo_usd
            u.saldo_usd = q2(u.saldo_usd - amount)
            usd_despues = u.saldo_usd

            usdt_bruto = amount * rate
            usdt_neto = q2(usdt_bruto * fee_factor)

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
                'comision_total_fmt': f"{q2(usdt_bruto-usdt_neto)} USDT" if usdt_bruto != usdt_neto else None,
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
            return True, None

        elif direccion == 'USDT_to_USD':
            if u.saldo_usdt < amount:
                return False, "Saldo USDT insuficiente"

            usdt_antes = u.saldo_usdt
            u.saldo_usdt = q2(u.saldo_usdt - amount)
            usdt_despues = u.saldo_usdt

            usd_bruto = amount / rate
            usd_neto = q2(usd_bruto * fee_factor)

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
                'comision_total_fmt': f"{q2(usd_bruto-usd_neto)} USD" if usd_bruto != usd_neto else None,
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
            return True, None

        return False, "Dirección de swap inválida"


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





@login_required
def verificar_boleto(request, numero):
    b = get_object_or_404(BoletoOperacion, numero=numero)

    # Seguridad: sólo el dueño o un admin puede ver
    if not (request.user == b.usuario or es_admin(request.user)):
        raise Http404()

    # Extraemos snapshot con tolerancia a tu modelo actual
    snapshot = getattr(b, 'snapshot', None)
    if not snapshot:
        data = getattr(b, 'data', {}) or {}
        snapshot = data.get('snapshot', {})  # fallback si lo guardaste así

    # Hash del PDF recomputado (si hay archivo guardado)
    pdf_ok = None
    recomputed_hex = None
    if hasattr(b, 'pdf') and b.pdf:
        pdf_bytes = b.pdf.read() if hasattr(b.pdf, 'read') else b.pdf.open('rb').read()
        recomputed_hex = hashlib.sha256(pdf_bytes).hexdigest().upper()
        stored_hex = getattr(b, 'pdf_sha256', None) or getattr(b, 'sha256', None)
        if stored_hex:
            pdf_ok = (recomputed_hex == stored_hex)

    ctx = {
        "boleto": b,
        "numero": b.numero,
        "fecha_emision": getattr(b, 'fecha_emision', localtime()),
        "tipo": b.get_tipo_display() if hasattr(b, "get_tipo_display") else b.tipo,
        "usuario": b.usuario,
        "snapshot": snapshot or {},
        "psav": (snapshot or {}).get("psav", {}),
        "cliente": (snapshot or {}).get("cliente", {}),
        "onchain": (getattr(b, 'data', {}) or {}).get("onchain", {}) if not snapshot else snapshot.get("onchain", {}),
        "pdf_sha256": getattr(b, 'pdf_sha256', None) or getattr(b, 'sha256', None),
        "recomputed_sha256": recomputed_hex,
        "pdf_ok": pdf_ok,  # True/False/None
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

@login_required
def soporte(request):
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER', '')

    if request.method == 'POST':
        asunto   = request.POST.get('asunto', '').strip()
        categoria= request.POST.get('categoria', '').strip()
        mensaje  = request.POST.get('mensaje', '').strip()
        next_post= request.POST.get('next') or next_url

        if not asunto or not mensaje:
            messages.error(request, "Completá asunto y mensaje.")
            return render(request, 'usuarios/soporte.html', {'next_url': next_url})

        # TODO: crear ticket / enviar mail / guardar en DB
        messages.success(request, "Tu consulta fue enviada. Te responderemos a la brevedad.")
        return redirect(next_post or 'dashboard')

    return render(request, 'usuarios/soporte.html', {'next_url': next_url})

def faq(request):
    """Preguntas frecuentes públicas."""
    return render(request, 'usuarios/faq.html')      # creá esta template si no existe


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

@login_required
def tyc(request):
    if request.method == "POST":
        request.user.marcar_tyc_aceptado(getattr(settings,"TYC_VERSION",""))
        messages.success(request, "Términos aceptados.")
        next_url = request.POST.get("next") or "dashboard"
        return redirect(next_url)
    return render(request, "usuarios/tyc.html", {"version": getattr(settings,"TYC_VERSION","")})