"""
Depósitos y retiros. Las transiciones de estado bloquean la fila del documento
(select_for_update) para que dos admins no puedan aprobar/enviar lo mismo dos veces.

Ciclo de un retiro en el mayor:
  solicitado: usuario -X  / retiros_pendientes +X
  enviado:    retiros_pendientes -X / tesoreria +X
  rechazado:  retiros_pendientes -X / usuario +X

Toda acción de un admin queda en RegistroAuditoria.
"""
import re
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils.timezone import localtime

from usuarios.models import (
    CuentaContable,
    DepositoARS,
    DepositoUSDT,
    RetiroARS,
    RetiroCrypto,
    Usuario,
)
from usuarios.services import avisos, compliance
from usuarios.services.auditoria import auditar
from usuarios.services.boletos import emitir_boleto
from usuarios.services.comun import (
    OperacionError,
    fmt_ccy,
    fmt_money,
    gen_numero_boleto,
    q_moneda,
    snapshot_boleto,
)
from usuarios.services.ledger import (
    CAMPO_SALDO,
    cuenta_sistema,
    cuenta_usuario,
    registrar_asiento,
)
from usuarios.templatetags.finanxis import formato_ar as fmt_ar
from usuarios.templatetags.finanxis import money
from usuarios.utils import registrar_movimiento


def _monto_fmt(moneda, monto):
    if moneda == 'USDT':
        return f"{money(monto)} USDT"
    return f"{'US$' if moneda == 'USD' else '$'} {money(monto)}"


def _bloquear(modelo, pk):
    return modelo.objects.select_for_update().get(pk=pk)


def _acreditar(u, moneda, monto):
    campo = CAMPO_SALDO[moneda]
    antes = getattr(u, campo)
    setattr(u, campo, q_moneda(antes + monto, moneda))
    u.save(update_fields=[campo])
    return antes, getattr(u, campo)


# ---------------- Depósitos ----------------

def _aprobar_deposito(modelo, deposito_id, moneda, admin, *, tipo_bol, detalle, onchain=None):
    dep = _bloquear(modelo, deposito_id)
    if dep.estado != 'pendiente':
        raise OperacionError("El depósito ya fue procesado.")
    u = _bloquear(Usuario, dep.usuario_id)

    antes, despues = _acreditar(u, moneda, dep.monto)
    dep.estado = 'aprobado'
    dep.save()

    mov = registrar_movimiento(
        usuario=u, tipo='deposito', moneda=moneda, monto=dep.monto,
        descripcion=detalle, admin=admin, saldo_antes=antes, saldo_despues=despues
    )
    registrar_asiento('deposito', detalle, [
        (cuenta_usuario(u), moneda, dep.monto),
        (cuenta_sistema(CuentaContable.TESORERIA), moneda, -dep.monto),
    ], referencia=f'{modelo.__name__}:{dep.pk}', creado_por=admin)

    fmt = fmt_money(dep.monto) if moneda == 'ARS' else fmt_ccy(dep.monto, moneda)
    snapshot = snapshot_boleto(
        u, titulo=f'Acreditación de depósito {moneda}', debitado=fmt, origen=fmt, destino=fmt,
        tasa=f"Red: {onchain['red']}" if onchain and onchain.get('red') else '—',
    )
    emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov, onchain=onchain)
    avisos.cliente(u, f"Acreditamos tu depósito de {fmt_ar(fmt)}.", asunto="Depósito acreditado",
                   detalle="Ya lo tenés disponible en tu saldo. El comprobante está en Comprobantes.",
                   url=avisos.url_absoluta('comprobantes'), boton="Ver comprobante")
    compliance.evaluar(u, descripcion=f'Depósito {moneda}', referencia=f'{modelo.__name__}:{dep.pk}',
                       monto_ars=compliance.valor_ars(moneda, dep.monto))
    auditar(admin, 'deposito_aprobado', dep, moneda=moneda, monto=dep.monto, usuario=u.pk)
    return dep


@transaction.atomic
def aprobar_deposito_ars(deposito_id, admin):
    """Depósito por transferencia bancaria, en pesos o en dólares."""
    moneda = DepositoARS.objects.values_list('moneda', flat=True).get(pk=deposito_id)
    return _aprobar_deposito(DepositoARS, deposito_id, moneda, admin,
                             tipo_bol='deposito_usd' if moneda == 'USD' else 'deposito_ars',
                             detalle='Depósito aprobado por admin')


@transaction.atomic
def aprobar_deposito_usdt(deposito_id, admin, *, forzar=False):
    """
    Solo se acredita si la transacción fue verificada on-chain. Un superusuario puede forzar
    la aprobación cuando la verificación no es posible (red no soportada, API caída), nunca
    cuando la verificación encontró que no coincide. Queda auditado.
    """
    dep = DepositoUSDT.objects.get(pk=deposito_id)
    if dep.verificacion_estado == 'no_coincide':
        raise OperacionError(f"La transacción no coincide on-chain: {dep.verificacion_detalle}")
    if dep.verificacion_estado != 'verificado':
        if not (forzar and admin.is_superuser):
            raise OperacionError("El depósito no está verificado on-chain. "
                                 "Reintentá la verificación o pedí a un superusuario que lo fuerce.")
        auditar(admin, 'deposito_usdt_forzado', dep, verificacion=dep.verificacion_estado,
                detalle=dep.verificacion_detalle, monto=dep.monto)
    onchain = {
        'red': dep.red,
        'origen': '',
        'destino': getattr(settings, 'WALLET_EMPRESA', ''),
        'txid': dep.txid,
        'fecha_hora': localtime().strftime('%d/%m/%Y %H:%M:%S'),
    }
    return _aprobar_deposito(
        DepositoUSDT, deposito_id, 'USDT', admin, tipo_bol='deposito_usdt', onchain=onchain,
        detalle=f'Depósito USDT aprobado por admin (red: {dep.red}, txid: {dep.txid})',
    )


@transaction.atomic
def rechazar_deposito(modelo, deposito_id, admin):
    dep = _bloquear(modelo, deposito_id)
    if dep.estado != 'pendiente':
        raise OperacionError("El depósito ya fue procesado.")
    dep.estado = 'rechazado'
    dep.save()

    moneda = 'USDT' if modelo is DepositoUSDT else dep.moneda
    extra = f' (txid: {dep.txid})' if moneda == 'USDT' else ''
    registrar_movimiento(
        usuario=dep.usuario, tipo='ajuste', moneda=moneda, monto=0, admin=admin,
        descripcion=f'Depósito {moneda} rechazado por admin. Monto solicitado: {dep.monto}{extra}'
    )
    simbolo = {'ARS': '$ ', 'USD': 'US$ '}.get(moneda, '')
    sufijo = ' USDT' if moneda == 'USDT' else ''
    avisos.cliente(dep.usuario, f"No pudimos acreditar tu depósito de {simbolo}{money(dep.monto)}{sufijo}.",
                   asunto="Depósito rechazado",
                   detalle="Revisá que el comprobante sea legible y coincida con el monto informado, o escribinos "
                           "a Soporte y lo vemos juntos.",
                   url=avisos.url_absoluta('soporte'), boton="Escribir a Soporte")
    auditar(admin, 'deposito_rechazado', dep, moneda=moneda, monto=dep.monto)
    return dep


# ---------------- Retiros ----------------

def _reservar_retiro(usuario, moneda, monto, descripcion, crear_documento):
    u = _bloquear(Usuario, usuario.pk)
    if getattr(u, CAMPO_SALDO[moneda]) < monto:
        raise OperacionError(f"Saldo {moneda} insuficiente.")
    monto_ars = compliance.valor_ars(moneda, monto)
    compliance.verificar_limite_retiro(u, monto_ars)

    retiro = crear_documento(u)
    antes, despues = _acreditar(u, moneda, -monto)
    registrar_movimiento(
        usuario=u, tipo='retiro', moneda=moneda, monto=-monto,
        descripcion=descripcion, saldo_antes=antes, saldo_despues=despues
    )
    registrar_asiento('retiro_solicitado', descripcion, [
        (cuenta_usuario(u), moneda, -monto),
        (cuenta_sistema(CuentaContable.RETIROS_PENDIENTES), moneda, monto),
    ], referencia=f'{type(retiro).__name__}:{retiro.pk}')
    compliance.evaluar_retiro(u, descripcion=f'Retiro {moneda}', monto_ars=monto_ars,
                              referencia=f'{type(retiro).__name__}:{retiro.pk}')
    monto_fmt = _monto_fmt(moneda, monto)
    avisos.cliente(u, f"Recibimos tu pedido de retiro de {monto_fmt}.", asunto="Pedido de retiro recibido",
                   detalle="El monto quedó reservado de tu saldo. Te avisamos cuando lo aprobemos y lo enviemos.")
    avisos.equipo('retiros', f"Nuevo retiro: {monto_fmt}",
                  f"Cliente: {avisos.cliente_desc(u)}\nMonto: {monto_fmt}\nDestino: {descripcion}",
                  url=avisos.url_absoluta('panel_admin') + '?tipo=' + ('retiro_crypto' if moneda == 'USDT' else 'retiro_ars'))
    return retiro


ALIAS_RE = re.compile(r'^[A-Za-z0-9.-]{6,20}$')
CBU_RE = re.compile(r'^\d{22}$')


@transaction.atomic
def solicitar_retiro_ars(usuario, monto, *, alias='', cbu='', banco='', moneda='ARS'):
    """Retiro por transferencia bancaria a una cuenta en pesos o en dólares."""
    if moneda not in ('ARS', 'USD'):
        raise OperacionError("Moneda inválida.")
    if not alias and not cbu:
        raise OperacionError("Ingresá el alias o el CBU/CVU de destino.")
    if alias and not ALIAS_RE.match(alias):
        raise OperacionError("El alias tiene que tener de 6 a 20 caracteres: letras, números, punto o guion.")
    if cbu and not CBU_RE.match(cbu):
        raise OperacionError("El CBU/CVU tiene que tener 22 números.")
    destino = alias or cbu
    return _reservar_retiro(
        usuario, moneda, monto, f'Solicitud de retiro {moneda} ({destino})',
        lambda u: RetiroARS.objects.create(usuario=u, moneda=moneda, alias=alias, cbu=cbu,
                                           banco=banco, monto=monto),
    )


REDES_RETIRO = ('TRC20', 'ERC20', 'BEP20')


@transaction.atomic
def solicitar_retiro_crypto(usuario, moneda, monto, wallet, red=''):
    # los dólares se retiran por transferencia bancaria (solicitar_retiro_ars con moneda USD)
    if moneda != 'USDT':
        raise OperacionError("Moneda inválida.")
    if red not in REDES_RETIRO:
        raise OperacionError("Elegí la red de destino.")
    if not wallet:
        raise OperacionError("Ingresá la dirección de destino.")
    retiro = _reservar_retiro(
        usuario, moneda, monto, f'Solicitud de retiro {moneda} - pendiente',
        lambda u: RetiroCrypto.objects.create(usuario=u, moneda=moneda, monto=monto, red=red,
                                              direccion_wallet=wallet, estado='pendiente'),
    )
    return retiro


@transaction.atomic
def aprobar_retiro_ars(retiro_id, admin=None):
    retiro = _bloquear(RetiroARS, retiro_id)
    if retiro.estado != 'pendiente':
        raise OperacionError("El retiro ya fue procesado.")
    retiro.estado = 'aprobado'
    retiro.save()
    auditar(admin, 'retiro_aprobado', retiro, moneda=retiro.moneda, monto=retiro.monto)
    avisos.cliente(retiro.usuario, f"Aprobamos tu retiro de {_monto_fmt(retiro.moneda, retiro.monto)}.",
                   asunto="Retiro aprobado", detalle="Lo transferimos en horario bancario y te avisamos al enviarlo.")
    return retiro


def _enviar_retiro(retiro, moneda, admin, *, descripcion, tipo_bol, tasa, onchain=None):
    u = retiro.usuario
    saldo = getattr(u, CAMPO_SALDO[moneda])
    # informativo: el saldo ya se descontó al solicitar
    mov = registrar_movimiento(
        usuario=u, tipo='retiro', moneda=moneda, monto=0, admin=admin,
        descripcion=descripcion, saldo_antes=saldo, saldo_despues=saldo
    )
    registrar_asiento('retiro_enviado', descripcion, [
        (cuenta_sistema(CuentaContable.RETIROS_PENDIENTES), moneda, -retiro.monto),
        (cuenta_sistema(CuentaContable.TESORERIA), moneda, retiro.monto),
    ], referencia=f'{type(retiro).__name__}:{retiro.pk}', creado_por=admin)

    fmt = fmt_money(retiro.monto) if moneda == 'ARS' else fmt_ccy(retiro.monto, moneda)
    snapshot = snapshot_boleto(u, titulo=f'Retiro {moneda} enviado', debitado=fmt, origen=fmt,
                               destino=fmt, tasa=tasa)
    emitir_boleto(u, tipo_bol, gen_numero_boleto(), snapshot, movimiento=mov, onchain=onchain)
    avisos.cliente(u, f"Enviamos tu retiro de {fmt_ar(fmt)}.", asunto="Retiro enviado",
                   detalle=f"{fmt_ar(tasa)}. El comprobante está en Comprobantes.",
                   url=avisos.url_absoluta('comprobantes'), boton="Ver comprobante")
    auditar(admin, 'retiro_enviado', retiro, moneda=moneda, monto=retiro.monto,
            destino=getattr(retiro, 'direccion_wallet', '') or getattr(retiro, 'alias', ''),
            txid=(onchain or {}).get('txid', ''))


@transaction.atomic
def enviar_retiro_ars(retiro_id, admin, *, titular_confirmado=False):
    retiro = _bloquear(RetiroARS, retiro_id)
    if retiro.estado != 'aprobado':
        raise OperacionError("Solo se pueden enviar retiros aprobados.")
    if not titular_confirmado:
        raise OperacionError("Confirmá que la cuenta de destino está a nombre del cliente. "
                             "Si no lo está, rechazá el retiro indicando el motivo.")
    retiro.titular_verificado = True
    retiro.estado = 'enviado'
    retiro.save()
    _enviar_retiro(
        retiro, retiro.moneda, admin, tipo_bol='retiro_usd' if retiro.moneda == 'USD' else 'retiro_ars',
        descripcion=(f'Retiro de {retiro.simbolo}{retiro.monto} {retiro.moneda} enviado por admin a '
                     f'{retiro.destino} ({retiro.banco or "s/banco"})'),
        tasa=f"Cuenta destino: {retiro.destino}{f' ({retiro.banco})' if retiro.banco else ''}",
    )
    return retiro


@transaction.atomic
def enviar_retiro_crypto(retiro_id, admin, *, red='', txid=''):
    retiro = _bloquear(RetiroCrypto, retiro_id)
    if retiro.estado != 'pendiente':
        raise OperacionError("El retiro ya fue procesado.")
    retiro.estado = 'enviado'
    retiro.admin_responsable = admin
    retiro.save()
    red = red or retiro.red  # la que eligió el cliente, salvo que el admin indique otra
    onchain = {
        'red': red,
        'origen': '(custodia interna)',
        'destino': retiro.direccion_wallet,
        'txid': txid,
        'fecha_hora': localtime().strftime("%d/%m/%Y %H:%M:%S"),
    }
    _enviar_retiro(
        retiro, retiro.moneda, admin, tipo_bol=f"retiro_{retiro.moneda.lower()}", onchain=onchain,
        descripcion=f'Retiro {retiro.moneda} aprobado por admin a {retiro.direccion_wallet}',
        tasa=f"Red: {red}" if red else '—',
    )
    return retiro


def _rechazar_retiro(retiro, moneda, admin, motivo):
    motivo = (motivo or '').strip()
    if not motivo:
        raise OperacionError("Indicá el motivo del rechazo: se lo informamos al cliente.")
    u = _bloquear(Usuario, retiro.usuario_id)
    antes, despues = _acreditar(u, moneda, retiro.monto)
    retiro.estado = 'rechazado'
    retiro.motivo_rechazo = motivo[:255]
    retiro.save()
    descripcion = f'Retiro {moneda} rechazado ({motivo}). Se recredita {retiro.monto}.'
    registrar_movimiento(
        usuario=u, tipo='ajuste', moneda=moneda, monto=retiro.monto, admin=admin,
        descripcion=descripcion, saldo_antes=antes, saldo_despues=despues
    )
    registrar_asiento('retiro_rechazado', descripcion, [
        (cuenta_sistema(CuentaContable.RETIROS_PENDIENTES), moneda, -retiro.monto),
        (cuenta_usuario(u), moneda, retiro.monto),
    ], referencia=f'{type(retiro).__name__}:{retiro.pk}', creado_por=admin)
    avisos.cliente(u, f"Rechazamos tu retiro de {_monto_fmt(moneda, retiro.monto)}: {motivo}. El monto volvió a tu saldo.",
                   asunto="Retiro rechazado", detalle="Si tenés dudas, escribinos a Soporte.",
                   url=avisos.url_absoluta('soporte'), boton="Escribir a Soporte")
    auditar(admin, 'retiro_rechazado', retiro, moneda=moneda, monto=retiro.monto, motivo=motivo)
    return retiro


@transaction.atomic
def rechazar_retiro_ars(retiro_id, admin, motivo=''):
    retiro = _bloquear(RetiroARS, retiro_id)
    if retiro.estado not in ('pendiente', 'aprobado'):
        raise OperacionError("El retiro ya fue procesado.")
    return _rechazar_retiro(retiro, retiro.moneda, admin, motivo)


@transaction.atomic
def rechazar_retiro_crypto(retiro_id, admin, motivo=''):
    retiro = _bloquear(RetiroCrypto, retiro_id)
    if retiro.estado != 'pendiente':
        raise OperacionError("El retiro ya fue procesado.")
    return _rechazar_retiro(retiro, retiro.moneda, admin, motivo)


# ---------------- Local físico (efectivo) ----------------

def _operacion_local(usuario, tipo, moneda, monto, admin, nota):
    from usuarios.models import OperacionLocal
    from usuarios.signals import _marcar_entrada, _marcar_salida

    if moneda not in ('ARS', 'USD', 'USDT'):
        raise OperacionError("Moneda inválida.")
    if monto <= 0:
        raise OperacionError("Monto inválido.")
    u = _bloquear(Usuario, usuario.pk)
    if u.estado_verificacion != 'aprobado' or u.declaracion_vigente is None:
        raise OperacionError("El cliente tiene que tener la identidad verificada y la declaración jurada completa.")
    monto_ars = compliance.valor_ars(moneda, monto)
    signo = Decimal(1) if tipo == 'ingreso' else Decimal(-1)
    if tipo == 'retiro':
        if getattr(u, CAMPO_SALDO[moneda]) < monto:
            raise OperacionError(f"Saldo {moneda} insuficiente.")
        compliance.verificar_limite_retiro(u, monto_ars)

    nombre = 'Ingreso' if tipo == 'ingreso' else 'Retiro'
    descripcion = f"{nombre} en efectivo en el local{f' ({nota})' if nota else ''}"
    antes, despues = _acreditar(u, moneda, signo * monto)
    mov = registrar_movimiento(
        usuario=u, tipo='deposito' if tipo == 'ingreso' else 'retiro', moneda=moneda, monto=signo * monto,
        descripcion=descripcion, admin=admin, saldo_antes=antes, saldo_despues=despues,
    )
    op = OperacionLocal.objects.create(usuario=u, tipo=tipo, moneda=moneda, monto=monto, nota=nota[:255],
                                       admin=admin, movimiento=mov)
    registrar_asiento(f'local_{tipo}', descripcion, [
        (cuenta_usuario(u), moneda, signo * monto),
        (cuenta_sistema(CuentaContable.TESORERIA), moneda, -signo * monto),
    ], referencia=f'OperacionLocal:{op.pk}', creado_por=admin)

    fmt = _monto_fmt(moneda, monto)
    snapshot = snapshot_boleto(u, titulo=f'{nombre} en efectivo ({moneda})', debitado=fmt, origen=fmt,
                               destino=fmt, tasa=f"Local físico{f' — {nota}' if nota else ''}")
    emitir_boleto(u, f'local_{tipo}', gen_numero_boleto(), snapshot, movimiento=mov)
    if tipo == 'ingreso':
        _marcar_entrada(importe=monto, moneda=moneda, instance=op, detalle=descripcion)
        compliance.evaluar(u, descripcion=f'Ingreso en efectivo {moneda}', monto_ars=monto_ars,
                           referencia=f'OperacionLocal:{op.pk}')
        avisos.cliente(u, f"Acreditamos {fmt} que ingresaste en el local.", asunto="Ingreso en efectivo acreditado",
                       detalle="Ya lo tenés disponible en tu saldo. El comprobante está en Comprobantes.",
                       url=avisos.url_absoluta('comprobantes'), boton="Ver comprobante")
    else:
        _marcar_salida(importe=monto, moneda=moneda, instance=op, detalle=descripcion)
        compliance.evaluar_retiro(u, descripcion=f'Retiro en efectivo {moneda}', monto_ars=monto_ars,
                                  referencia=f'OperacionLocal:{op.pk}')
        avisos.cliente(u, f"Retiraste {fmt} en efectivo en el local.", asunto="Retiro en efectivo",
                       detalle="El comprobante está en Comprobantes.",
                       url=avisos.url_absoluta('comprobantes'), boton="Ver comprobante")
    auditar(admin, f'local_{tipo}', op, moneda=moneda, monto=monto, usuario=u.pk, nota=nota)
    return op


@transaction.atomic
def ingreso_local(usuario, moneda, monto, admin, nota=''):
    return _operacion_local(usuario, 'ingreso', moneda, monto, admin, nota)


@transaction.atomic
def retiro_local(usuario, moneda, monto, admin, nota=''):
    return _operacion_local(usuario, 'retiro', moneda, monto, admin, nota)

