"""
Convierte movimientos contables en actividades legibles para el cliente.

Una compra, venta o swap genera dos movimientos (una moneda sale, otra entra) unidos por
`Movimiento.operacion`; acá se muestran como una sola actividad con una frase clara.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from usuarios.models import BoletoOperacion

NOMBRE = {'ARS': 'pesos', 'USD': 'dólares', 'USDT': 'USDT'}
TIPOS = (
    ('deposito', 'Ingresos'),
    ('retiro', 'Retiros'),
    ('compra', 'Compras'),
    ('venta', 'Ventas'),
    ('swap', 'Conversiones'),
)
_MONTO_EN_TEXTO = re.compile(r'monto(?: solicitado)?:\s*\$?\s*([0-9]+(?:\.[0-9]+)?)', re.I)
_DESTINO_RETIRO = re.compile(r'\(([^)]+)\)\s*$')


@dataclass
class Actividad:
    clave: str
    tipo: str                 # deposito | retiro | compra | venta | swap | ajuste
    fecha: object
    titulo: str
    detalle: str = ''
    monto: Decimal | None = None     # importe principal, con signo
    moneda: str = ''
    estado: str = ''          # texto corto para movimientos informativos ("En revisión")
    movimientos: list = field(default_factory=list)
    boleto: object = None

    @property
    def monedas(self):
        return {m.moneda for m in self.movimientos}

    @property
    def positivo(self):
        return self.monto is not None and self.monto > 0


def _fmt(valor, moneda):
    v = abs(Decimal(valor))
    texto = f"{v:,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')
    return f"$ {texto}" if moneda == 'ARS' else f"{texto} {moneda}"


def _monto_en_texto(descripcion):
    m = _MONTO_EN_TEXTO.search(descripcion or '')
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def _operacion(movs):
    """Compra, venta o swap a partir de sus dos patas."""
    sale = next((m for m in movs if m.monto < 0), movs[0])
    entra = next((m for m in movs if m.monto > 0), movs[-1])
    fecha = max(m.fecha for m in movs)
    clave = str(movs[0].operacion or movs[0].codigo)

    if sale.moneda != 'ARS' and entra.moneda != 'ARS':
        return Actividad(clave, 'swap', fecha,
                         f"Convertiste {NOMBRE[sale.moneda]} a {NOMBRE[entra.moneda]}",
                         f"Entregaste {_fmt(sale.monto, sale.moneda)}",
                         entra.monto, entra.moneda, movimientos=movs)
    if sale.moneda == 'ARS':
        precio = abs(sale.monto) / entra.monto if entra.monto else None
        detalle = f"Pagaste {_fmt(sale.monto, 'ARS')}"
        if precio:
            detalle += f" a {_fmt(precio, 'ARS')}"
        return Actividad(clave, 'compra', fecha, f"Compraste {NOMBRE[entra.moneda]}", detalle,
                         entra.monto, entra.moneda, movimientos=movs)
    precio = entra.monto / abs(sale.monto) if sale.monto else None
    detalle = f"Entregaste {_fmt(sale.monto, sale.moneda)}"
    if precio:
        detalle += f" a {_fmt(precio, 'ARS')}"
    return Actividad(clave, 'venta', fecha, f"Vendiste {NOMBRE[sale.moneda]}", detalle,
                     entra.monto, 'ARS', movimientos=movs)


def _simple(m):
    """Depósitos, retiros y ajustes (un solo movimiento)."""
    desc = m.descripcion or ''
    nombre = NOMBRE.get(m.moneda, m.moneda)
    a = Actividad(str(m.codigo), m.tipo, m.fecha, '', monto=m.monto or None, moneda=m.moneda, movimientos=[m])

    if m.tipo == 'deposito':
        if m.monto:
            a.titulo = f"Ingresaste {nombre}"
        else:
            a.titulo = f"Informaste un depósito en {nombre}"
            pedido = _monto_en_texto(desc)
            a.detalle = f"Por {_fmt(pedido, m.moneda)}" if pedido else ''
            a.estado = 'En revisión'
    elif m.tipo == 'retiro':
        if m.monto:
            a.titulo = f"Pediste un retiro de {nombre}"
            destino = _DESTINO_RETIRO.search(desc)
            a.detalle = f"A {destino.group(1)}" if destino else ''
        else:
            a.titulo = f"Enviamos tu retiro de {nombre}"
            a.estado = 'Enviado'
    elif 'rechazado' in desc.lower() and 'retiro' in desc.lower():
        a.tipo = 'retiro'
        a.titulo = f"Retiro de {nombre} rechazado"
        a.detalle = 'Te devolvimos el saldo'
    elif 'rechazado' in desc.lower():
        a.tipo = 'deposito'
        a.titulo = f"Depósito en {nombre} rechazado"
        pedido = _monto_en_texto(desc)
        a.detalle = f"Por {_fmt(pedido, m.moneda)}" if pedido else ''
        a.estado = 'Rechazado'
    else:
        a.titulo = 'Ajuste de saldo'
        a.detalle = desc
    return a


def agrupar(movimientos):
    """
    Movimientos (cualquier orden) → actividades, de la más reciente a la más vieja.
    Las patas de una misma operación se juntan aunque vengan separadas.
    """
    movimientos = list(movimientos)
    por_operacion, actividades = {}, []
    for m in movimientos:
        if m.operacion:
            por_operacion.setdefault(m.operacion, []).append(m)
        else:
            actividades.append(_simple(m))
    for movs in por_operacion.values():
        movs.sort(key=lambda x: x.id)
        if len(movs) == 1 and movs[0].tipo not in ('compra', 'venta'):
            actividades.append(_simple(movs[0]))
        elif len(movs) == 1:
            # la otra pata quedó fuera del filtro: se muestra igual, sin el detalle de la contraparte
            m = movs[0]
            verbo = 'Compraste' if m.monto > 0 else 'Vendiste'
            actividades.append(Actividad(str(m.operacion), m.tipo, m.fecha, f"{verbo} {NOMBRE.get(m.moneda, m.moneda)}",
                                         monto=m.monto, moneda=m.moneda, movimientos=movs))
        else:
            actividades.append(_operacion(movs))

    # Boleto de cada actividad (uno por operación), en una sola consulta
    ids = [m.pk for m in movimientos]
    boletos = {b.movimiento_id: b for b in BoletoOperacion.objects.filter(movimiento_id__in=ids, anulado=False)}
    for a in actividades:
        a.boleto = next((boletos[m.pk] for m in a.movimientos if m.pk in boletos), None)

    actividades.sort(key=lambda a: (a.fecha, max(m.id for m in a.movimientos)), reverse=True)
    return actividades
