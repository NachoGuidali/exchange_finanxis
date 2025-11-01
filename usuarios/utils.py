from usuarios.models import Notificacion


def registrar_movimiento(usuario, tipo, moneda, monto, descripcion="", admin=None, saldo_antes=None, saldo_despues=None):
    from .models import Movimiento

    Movimiento.objects.create(
        usuario=usuario,
        tipo=tipo,
        moneda=moneda,
        monto=monto,
        descripcion=descripcion,
        admin_responsable=admin,
        saldo_antes=saldo_antes,
        saldo_despues=saldo_despues
    )


def crear_notificacion(usuario, mensaje):
    Notificacion.objects.create(usuario=usuario, mensaje=mensaje)


def cliente_ctx(u):
    nombre = (f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}").strip() or u.username
    return {
        "nombre": nombre,
        "doc_tipo": getattr(u, "doc_tipo", "DNI"),
        "doc_nro":  getattr(u, "doc_nro", "—"),
        "domicilio": getattr(u, "domicilio", "—"),
        "email": getattr(u, "email", "") or "—",
        "telefono": getattr(u, "telefono", "—"),
        "estado_verificacion": getattr(u, "estado_verificacion", "pendiente"),
    }    


