"""Compra, venta y swap (la lógica vive en services/trading.py)."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from django.shortcuts import redirect, render

from usuarios.services import compliance, cotizaciones, trading
from usuarios.services.comun import OperacionError

from ..models import Cotizacion, ExchangeConfig
from ..utils import MONEDAS_CRYPTO, cotizacion_vigente, kyc_required, parse_monto

logger = logging.getLogger(__name__)


@login_required
@kyc_required
def operar(request):
    cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
    cot_usd  = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
    if not cot_usdt or not cot_usd:
        return HttpResponse("No hay cotización disponible. Intentá más tarde.", status=503)
    cotizaciones_vigentes = cotizacion_vigente(cot_usdt) and cotizacion_vigente(cot_usd)
    if settings.COTIZACION_ACTUALIZAR_AL_VUELO and cotizaciones.actualizar_si_vencida(cotizaciones_vigentes):
        cot_usdt = Cotizacion.objects.filter(moneda='USDT').order_by('-fecha').first()
        cot_usd  = Cotizacion.objects.filter(moneda='USD').order_by('-fecha').first()
        cotizaciones_vigentes = cotizacion_vigente(cot_usdt) and cotizacion_vigente(cot_usd)
    cfg = ExchangeConfig.current()

    if request.method == 'POST':
        operacion = request.POST.get('operacion')

        if operacion in ('compra', 'venta'):
            if not cotizaciones_vigentes:
                messages.error(request, "La cotización está desactualizada. Intentá de nuevo en unos minutos.")
                return redirect('operar')
            moneda = request.POST.get('moneda')
            if moneda not in MONEDAS_CRYPTO:
                return HttpResponse("Moneda inválida.", status=400)
            monto = parse_monto(request.POST.get('monto'))
            if monto is None:
                return HttpResponse("Monto inválido.", status=400)

            cot = cot_usdt if moneda == 'USDT' else cot_usd
            try:
                reserva = trading.cotizar(request.user, operacion, moneda, monto, cot)
            except compliance.LimiteExcedido as e:
                compliance.alerta_limite(request.user, e)
                messages.error(request, str(e))
                return redirect('operar')
            except OperacionError as e:
                messages.error(request, str(e))
                return redirect('operar')
            # el cliente confirma viendo el precio exacto, reservado unos segundos
            return render(request, 'usuarios/confirmar_operacion.html', {'reserva': reserva})

        elif operacion == 'swap':
            amount = parse_monto(request.POST.get('monto'))
            if amount is None:
                messages.error(request, "Monto inválido.")
                return redirect('dashboard')
            try:
                trading.swap(request.user, request.POST.get('swap_direccion'), amount, fee_bps=cfg.swap_fee_bps)
            except OperacionError as e:
                messages.error(request, str(e))
                return redirect('dashboard')

            messages.success(request, "Swap realizado con éxito.")
            return redirect('dashboard')

        return HttpResponse("Operación no válida", status=400)

    return render(request, 'usuarios/operar.html', {
        'cot_usdt': {'compra': cot_usdt.compra, 'venta': cot_usdt.venta},
        'cot_usd':  {'compra': cot_usd.compra,  'venta': cot_usd.venta},
        'swap_fee_bps': cfg.swap_fee_bps,
        'swap_rate': trading.SWAP_RATE,
        'cotizaciones_vigentes': cotizaciones_vigentes,
    })


@login_required
@kyc_required
@require_POST
def confirmar_operacion(request):
    try:
        trading.ejecutar_cotizacion(request.user, request.POST.get('reserva'))
    except compliance.LimiteExcedido as e:
        compliance.alerta_limite(request.user, e)
        messages.error(request, str(e))
        return redirect('operar')
    except (OperacionError, ValidationError) as e:
        messages.error(request, e.messages[0] if isinstance(e, ValidationError) else str(e))
        return redirect('operar')
    except Exception:
        logger.exception("Error confirmando operación (usuario %s)", request.user.pk)
        messages.error(request, "Ocurrió un error al procesar la operación.")
        return redirect('operar')

    messages.success(request, "Operación realizada con éxito.")
    return redirect('dashboard')
