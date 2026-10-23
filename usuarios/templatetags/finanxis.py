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
