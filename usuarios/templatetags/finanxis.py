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
