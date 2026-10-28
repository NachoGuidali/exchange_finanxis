"""
Tickets de soporte: el cliente abre y responde; el equipo responde y cambia el estado.
Cada respuesta del equipo avisa al cliente (notificación + email) y cada respuesta del
cliente reabre el ticket y avisa al equipo (destinatarios de "Soporte" en Parámetros).
"""
from django.utils import timezone

from usuarios.models import RespuestaTicket, SupportTicket
from usuarios.services import avisos
from usuarios.services.auditoria import auditar
from usuarios.utils import crear_notificacion


def avisar_ticket_nuevo(ticket):
    avisos.equipo('soporte', f"Nueva consulta #{ticket.pk}: {ticket.asunto}",
                  f"Cliente: {avisos.cliente_desc(ticket.usuario)} ({ticket.email})\n"
                  f"Tema: {ticket.get_categoria_display()}\nUrgencia: {ticket.get_prioridad_display()}\n\n"
                  f"{ticket.mensaje}",
                  url=avisos.url_absoluta('admin_ticket', ticket.pk), boton="Responder")
    avisos.enviar_email([ticket.email], f"Recibimos tu consulta #{ticket.pk}",
                        f"Hola {ticket.usuario.first_name or ticket.usuario.username}:\n\n"
                        f"Recibimos tu consulta \"{ticket.asunto}\" y te vamos a responder a la brevedad.",
                        boton_url=avisos.url_absoluta('ver_ticket', ticket.pk), boton_texto="Ver mi consulta")


def responder(ticket, autor, mensaje, *, es_staff):
    mensaje = (mensaje or '').strip()
    if not mensaje:
        raise ValueError("Escribí un mensaje.")
    if ticket.estado == 'cerrado' and not es_staff:
        raise ValueError("Este ticket está cerrado. Abrí uno nuevo si necesitás ayuda.")

    respuesta = RespuestaTicket.objects.create(ticket=ticket, autor=autor, es_staff=es_staff, mensaje=mensaje)
    ticket.estado = 'respondido' if es_staff else 'abierto'
    ticket.actualizado_en = timezone.now()
    ticket.save(update_fields=['estado', 'actualizado_en'])

    if es_staff:
        crear_notificacion(ticket.usuario, f"Respondimos tu consulta #{ticket.pk}: {ticket.asunto}")
        avisos.enviar_email([ticket.email], f"Respuesta a tu consulta #{ticket.pk}: {ticket.asunto}", mensaje,
                            titulo=f"Respondimos tu consulta #{ticket.pk}",
                            boton_url=avisos.url_absoluta('ver_ticket', ticket.pk), boton_texto="Responder")
        auditar(autor, 'ticket_respondido', ticket)
    else:
        avisos.equipo('soporte', f"El cliente respondió la consulta #{ticket.pk}: {ticket.asunto}",
                      f"Cliente: {avisos.cliente_desc(ticket.usuario)}\n\n{mensaje}",
                      url=avisos.url_absoluta('admin_ticket', ticket.pk), boton="Responder")
    return respuesta


def cambiar_estado(ticket, estado, admin):
    if estado not in dict(SupportTicket.ESTADOS):
        raise ValueError("Estado inválido.")
    anterior = ticket.estado
    ticket.estado = estado
    ticket.actualizado_en = timezone.now()
    ticket.save(update_fields=['estado', 'actualizado_en'])
    auditar(admin, 'ticket_estado', ticket, anterior=anterior, nuevo=estado)
    if estado == 'cerrado' and anterior != 'cerrado':
        crear_notificacion(ticket.usuario, f"Cerramos tu consulta #{ticket.pk}: {ticket.asunto}")
    return ticket
