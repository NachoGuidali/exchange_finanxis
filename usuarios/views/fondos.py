"""Solicitudes de depósito y retiro del usuario (lógica en services/fondos.py)."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from usuarios.services import compliance, fondos
from usuarios.services.comun import OperacionError

from ..forms import DepositoARSForm, DepositoUSDTForm
from ..utils import kyc_required, parse_monto, registrar_movimiento

logger = logging.getLogger(__name__)


@login_required
@kyc_required
def agregar_saldo(request):
    if request.method == 'POST':
        form = DepositoARSForm(request.POST, request.FILES)
        if form.is_valid():
            deposito = form.save(commit=False)
            deposito.usuario = request.user
            deposito.estado = 'pendiente'
            deposito.save()

            # Movimiento informativo (no cambia saldos): el monto real se registra al aprobar
            saldo_actual = request.user.saldo_ars
            registrar_movimiento(
                usuario=request.user,
                tipo='deposito',
                moneda='ARS',
                monto=0,
                descripcion=f'Solicitud de depósito ARS enviada (monto: ${deposito.monto}). En revisión.',
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
@kyc_required
def depositar_usdt(request):

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
            transaction.on_commit(lambda: _programar_verificacion(dep.pk))
            messages.success(request, 'Solicitud enviada. Verificamos la transacción en la red y te avisamos.')
            return redirect('dashboard')
    else:
        form = DepositoUSDTForm()

    wallets = [(red, w) for red, w in settings.WALLETS_EMPRESA.items() if w]
    return render(request, 'usuarios/depositar_usdt.html', {'form': form, 'wallets': wallets})


def _programar_verificacion(deposito_id):
    """Encola la verificación on-chain. Si Celery no está disponible, el admin puede reintentarla."""
    from usuarios.tasks import verificar_deposito_usdt_task
    try:
        verificar_deposito_usdt_task.apply_async(args=[deposito_id], retry=False)
    except Exception:
        logger.warning("No se pudo encolar la verificación on-chain del depósito %s", deposito_id, exc_info=True)


@login_required
@kyc_required
def solicitar_retiro(request):
    if request.method == 'POST':
        monto = parse_monto(request.POST.get('monto'))
        if monto is None:
            messages.error(request, "Monto inválido.")
            return redirect('solicitar_retiro')
        try:
            fondos.solicitar_retiro_ars(
                request.user, monto,
                alias=(request.POST.get('alias') or '').strip(),
                cbu=(request.POST.get('cbu') or '').strip(),
                banco=(request.POST.get('banco') or '').strip(),
            )
        except compliance.LimiteExcedido as e:
            compliance.alerta_limite(request.user, e)
            messages.error(request, str(e))
            return redirect('solicitar_retiro')
        except OperacionError as e:
            messages.error(request, str(e))
            return redirect('solicitar_retiro')

        messages.success(request, f"Solicitud de retiro enviada: ${monto} ARS")
        return redirect('dashboard')

    return render(request, 'usuarios/solicitar_retiro.html')


@login_required
@kyc_required
@require_POST
def solicitar_retiro_cripto(request):
    moneda = request.POST.get('moneda')
    monto = parse_monto(request.POST.get('monto'))
    if monto is None:
        messages.error(request, "Monto inválido.")
        return redirect('solicitar_retiro')
    try:
        fondos.solicitar_retiro_crypto(
            request.user, moneda, monto, (request.POST.get('direccion_wallet') or '').strip()
        )
    except compliance.LimiteExcedido as e:
        compliance.alerta_limite(request.user, e)
        messages.error(request, str(e))
        return redirect('solicitar_retiro')
    except OperacionError as e:
        messages.error(request, str(e))
        return redirect('solicitar_retiro')

    messages.success(request, f"Solicitud de retiro enviada: {monto} {moneda}")
    return redirect('dashboard')
