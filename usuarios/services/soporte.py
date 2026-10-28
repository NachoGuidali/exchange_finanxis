"""
Tickets de soporte: el cliente abre y responde; el equipo responde y cambia el estado.
Cada respuesta del equipo avisa al cliente (notificación + email) y cada respuesta del
cliente reabre el ticket y avisa al equipo.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from usuarios.models import RespuestaTicket, SupportTicket
from usuarios.services.auditoria import auditar
from usuarios.utils import crear_notificacion

logger = logging.getLogger(__name__)


def _url(path):
    return settings.SITE_URL.rstrip('/') + path


def _mail(asunto, cuerpo, destinatarios):
    destinatarios = [d for d in destinatarios if d]
    if not destinatarios:
        return
    try:
        send_mail(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, destinatarios, fail_silently=False)
    except Exception:
        # un mail caído no puede impedir registrar la respuesta
        logger.warning("No se pudo enviar el mail de soporte '%s'", asunto, exc_info=True)


def avisar_ticket_nuevo(ticket):
    empresa = settings.EMPRESA_NOMBRE
    _mail(f"[{empresa}] Nuevo ticket #{ticket.pk}: {ticket.asunto}",
          f"Cliente: {ticket.usuario.get_full_name() or ticket.usuario.username} ({ticket.email})\n"
          f"Categoría: {ticket.get_categoria_display()}\nPrioridad: {ticket.get_prioridad_display()}\n\n"
          f"{ticket.mensaje}\n\nResponder: {_url(reverse('admin_ticket', args=[ticket.pk]))}",
          settings.SOPORTE_TO)
    _mail(f"Recibimos tu consulta #{ticket.pk}",
          f"Hola {ticket.usuario.first_name or ticket.usuario.username},\n\n"
          f"Recibimos tu consulta \"{ticket.asunto}\" y te vamos a responder a la brevedad.\n"
          f"Podés seguirla en {_url(reverse('ver_ticket', args=[ticket.pk]))}\n\n— Soporte {empresa}",
          [ticket.email])


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

    empresa = settings.EMPRESA_NOMBRE
    if es_staff:
        crear_notificacion(ticket.usuario, f"Respondimos tu consulta #{ticket.pk}: {ticket.asunto}")
        _mail(f"Respuesta a tu consulta #{ticket.pk}: {ticket.asunto}",
              f"{mensaje}\n\nPodés responder desde {_url(reverse('ver_ticket', args=[ticket.pk]))}\n\n— Soporte {empresa}",
              [ticket.email])
        auditar(autor, 'ticket_respondido', ticket)
    else:
        _mail(f"[{empresa}] El cliente respondió el ticket #{ticket.pk}: {ticket.asunto}",
              f"{mensaje}\n\nVer: {_url(reverse('admin_ticket', args=[ticket.pk]))}",
              settings.SOPORTE_TO)
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
