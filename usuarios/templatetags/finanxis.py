import re
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(valor, decimales=2):
    """1234567.5 → '1.234.567,50' (formato argentino, sin depender de USE_THOUSAND_SEPARATOR)."""
    try:
        d = Decimal(str(valor if valor not in (None, "") else 0))
        texto = f"{d:,.{int(decimales)}f}"
    except (InvalidOperation, ValueError, TypeError):
        return valor
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


@register.filter
def bps_pct(valor):
    """200 → '2%', 150 → '1,5%' (puntos básicos a porcentaje)."""
    try:
        return f"{Decimal(str(valor)) / 100:g}".replace(".", ",") + "%"
    except (InvalidOperation, ValueError, TypeError):
        return valor


@register.filter
def absoluto(valor):
    try:
        return abs(Decimal(str(valor)))
    except (InvalidOperation, ValueError, TypeError):
        return valor


MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre")


@register.filter
def dia_relativo(fecha):
    """'Hoy', 'Ayer', '20 de octubre' o '20 de octubre de 2025' si es de otro año."""
    from datetime import timedelta

    from django.utils.timezone import localdate, localtime

    try:
        dia = localtime(fecha).date()
    except (ValueError, TypeError, AttributeError):
        return ""
    hoy = localdate()
    if dia == hoy:
        return "Hoy"
    if dia == hoy - timedelta(days=1):
        return "Ayer"
    texto = f"{dia.day} de {MESES[dia.month - 1]}"
    return texto if dia.year == hoy.year else f"{texto} de {dia.year}"


@register.filter
def hace(fecha):
    """Antigüedad con una sola unidad: '5 minutos', '2 horas', '3 días'."""
    from django.utils.timesince import timesince

    try:
        return timesince(fecha, depth=1)
    except (ValueError, TypeError):
        return ""


_NUMERO_EN = re.compile(r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\$?\d+\.\d+|\$\d+")


@register.filter
def formato_ar(texto):
    """'$1,234.56' → '$ 1.234,56' y '120.50 USDT' → '120,50 USDT' (textos guardados en los boletos)."""
    if not texto:
        return texto

    def convertir(m):
        t = m.group(0)
        signo = "$ " if t.startswith("$") else ""
        t = t.lstrip("$").replace(",", "")
        decimales = len(t.split(".")[1]) if "." in t else 0
        return signo + money(t, decimales)
    return _NUMERO_EN.sub(convertir, str(texto))
