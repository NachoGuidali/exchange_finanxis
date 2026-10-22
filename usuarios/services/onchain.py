"""
Verificación on-chain de depósitos USDT: que el TXID exista, esté confirmado, sea una
transferencia del contrato oficial de USDT hacia NUESTRA wallet y por al menos el monto declarado.

Redes: TRC20 (TronGrid), ERC20 y BEP20 (JSON-RPC estándar). Solana queda como no soportada
(requiere aprobación manual forzada por un superusuario).
"""
import hashlib
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

TRANSFER_TOPIC = 'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

REDES = {
    # red: contrato USDT, decimales, confirmaciones mínimas
    'TRC20': {'contrato': 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', 'decimales': 6, 'confirmaciones': 20},
    'ERC20': {'contrato': '0xdAC17F958D2ee523a2206206994597C13D831ec7', 'decimales': 6, 'confirmaciones': 12},
    'BEP20': {'contrato': '0x55d398326f99059fF775485246999027B3197955', 'decimales': 18, 'confirmaciones': 15},
}
TIMEOUT = 15
# Algunos RPC públicos bloquean el User-Agent por defecto de requests
HEADERS = {'User-Agent': 'finanxis-verificador/1.0', 'Content-Type': 'application/json'}


@dataclass
class Resultado:
    estado: str          # verificado | pendiente | no_coincide | no_soportada | error
    detalle: str
    monto: Decimal | None = None


class _ErrorConsulta(Exception):
    pass


# ---------------- utilidades de direcciones ----------------

_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def tron_a_hex(direccion):
    """Dirección TRON base58check (T...) → 20 bytes en hex, sin el prefijo 41."""
    n = 0
    for c in direccion:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes(25, 'big')
    payload, checksum = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum or payload[0] != 0x41:
        raise ValueError(f"Dirección TRON inválida: {direccion}")
    return payload[1:].hex()


def _addr20(valor):
    """Últimos 20 bytes (40 hex) de un topic/dirección, en minúsculas."""
    return valor.lower().removeprefix('0x')[-40:]


def _wallet_empresa(red):
    return (getattr(settings, 'WALLETS_EMPRESA', {}) or {}).get(red, '')


# ---------------- consultas ----------------

def _rpc(url, metodo, params):
    try:
        r = requests.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': metodo, 'params': params},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise _ErrorConsulta(f"{metodo}: {e}") from e
    if 'error' in data:
        raise _ErrorConsulta(f"{metodo}: {data['error']}")
    return data.get('result')


def _tron(path, payload=None):
    headers = dict(HEADERS)
    if getattr(settings, 'TRONGRID_API_KEY', ''):
        headers['TRON-PRO-API-KEY'] = settings.TRONGRID_API_KEY
    try:
        r = requests.post(settings.TRONGRID_URL.rstrip('/') + path, json=payload or {},
                          headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise _ErrorConsulta(f"TronGrid {path}: {e}") from e


def _transferencias_evm(red, txid):
    """(confirmaciones, [(contrato, destino, monto_crudo)]) o None si la tx no existe todavía."""
    url = settings.RPC_URLS[red]
    recibo = _rpc(url, 'eth_getTransactionReceipt', [txid])
    if not recibo:
        return None
    if recibo.get('status') != '0x1':
        return 'fallida'
    actual = int(_rpc(url, 'eth_blockNumber', []), 16)
    confirmaciones = actual - int(recibo['blockNumber'], 16) + 1
    transfers = [
        (_addr20(log['address']), _addr20(log['topics'][2]), int(log['data'], 16))
        for log in recibo.get('logs', [])
        if len(log.get('topics', [])) == 3 and log['topics'][0].lower().removeprefix('0x') == TRANSFER_TOPIC
    ]
    return confirmaciones, transfers


def _transferencias_tron(txid):
    info = _tron('/wallet/gettransactioninfobyid', {'value': txid})
    if not info or 'blockNumber' not in info:
        return None
    if (info.get('receipt') or {}).get('result') != 'SUCCESS':
        return 'fallida'
    actual = _tron('/wallet/getnowblock')['block_header']['raw_data']['number']
    confirmaciones = actual - info['blockNumber'] + 1
    transfers = [
        (_addr20(log['address']), _addr20(log['topics'][2]), int(log['data'], 16))
        for log in info.get('log', [])
        if len(log.get('topics', [])) == 3 and log['topics'][0].lower() == TRANSFER_TOPIC
    ]
    return confirmaciones, transfers


# ---------------- API pública ----------------

def verificar(red, txid, monto_declarado):
    """Consulta la red y devuelve un Resultado. No modifica la base."""
    if red not in REDES:
        return Resultado('no_soportada', f"La red {red} no tiene verificación automática.")
    wallet = _wallet_empresa(red)
    if not wallet:
        return Resultado('error', f"No hay wallet de la empresa configurada para {red}.")
    conf = REDES[red]
    txid = txid.strip()

    try:
        if red == 'TRC20':
            datos = _transferencias_tron(txid.removeprefix('0x'))
            contrato, destino = tron_a_hex(conf['contrato']), tron_a_hex(wallet)
        else:
            datos = _transferencias_evm(red, txid if txid.startswith('0x') else '0x' + txid)
            contrato, destino = _addr20(conf['contrato']), _addr20(wallet)
    except _ErrorConsulta as e:
        return Resultado('error', str(e)[:255])
    except ValueError as e:
        return Resultado('error', str(e)[:255])

    if datos is None:
        return Resultado('pendiente', "La transacción todavía no aparece en la red.")
    if datos == 'fallida':
        return Resultado('no_coincide', "La transacción falló en la red.")

    confirmaciones, transfers = datos
    crudo = sum(m for c, d, m in transfers if c == contrato and d == destino)
    if crudo == 0:
        return Resultado('no_coincide', "La transacción no transfiere USDT a la wallet de la empresa.")
    monto = (Decimal(crudo) / (Decimal(10) ** conf['decimales'])).quantize(Decimal('0.000001'))

    if confirmaciones < conf['confirmaciones']:
        return Resultado('pendiente', f"{confirmaciones}/{conf['confirmaciones']} confirmaciones.", monto)
    if monto < Decimal(monto_declarado):
        return Resultado('no_coincide', f"On-chain {monto} USDT, declarado {monto_declarado}.", monto)
    return Resultado('verificado', f"{monto} USDT recibidos con {confirmaciones} confirmaciones.", monto)


def verificar_deposito(deposito):
    """Verifica y guarda el resultado en el DepositoUSDT."""
    res = verificar(deposito.red, deposito.txid, deposito.monto)
    deposito.verificacion_estado = res.estado
    deposito.verificacion_detalle = res.detalle[:255]
    deposito.monto_onchain = res.monto
    deposito.verificado_en = timezone.now()
    deposito.save(update_fields=['verificacion_estado', 'verificacion_detalle', 'monto_onchain', 'verificado_en'])
    return res
