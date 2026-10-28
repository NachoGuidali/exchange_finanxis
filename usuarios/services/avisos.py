"""
Avisos al cliente (notificación en la app + email) y al equipo (email por tipo de evento).

Los destinatarios del equipo se configuran en Parámetros (ExchangeConfig.avisos_*). El remitente
y el servidor de correo salen del .env (DEFAULT_FROM_EMAIL, EMAIL_*). Los emails se mandan después
del commit: si la operación se revierte, no sale ningún aviso. Un mail que falla nunca rompe la
operación: queda en el log.
"""
import logging
import re

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse

from usuarios.models import ExchangeConfig, Notificacion

logger = logging.getLogger(__name__)

EVENTOS_EQUIPO = {
    'depositos': 'avisos_depositos',
    'retiros': 'avisos_retiros',
    'identidades': 'avisos_identidades',
    'compliance': 'avisos_compliance',
    'soporte': 'avisos_soporte',
}


def url_absoluta(nombre, *args):
    return settings.SITE_URL.rstrip('/') + reverse(nombre, args=args)


def destinatarios(evento):
    texto = getattr(ExchangeConfig.current(), EVENTOS_EQUIPO[evento], '') or ''
    emails = [e for e in re.split(r'[,;\s]+', texto) if e]
    if not emails and evento == 'soporte':
        emails = list(getattr(settings, 'SOPORTE_TO', []))
    return emails


def _armar(para, asunto, texto, titulo=None, boton_url=None, boton_texto=None):
    ctx = {
        'empresa': settings.EMPRESA_NOMBRE, 'titulo': titulo or asunto, 'texto': texto,
        'boton_url': boton_url, 'boton_texto': boton_texto,
    }
    cuerpo = texto + (f"\n\n{boton_texto}: {boton_url}" if boton_url else "") + f"\n\n— {settings.EMPRESA_NOMBRE}"
    mail = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, to=para)
    mail.attach_alternative(render_to_string('emails/aviso.html', ctx), 'text/html')
    return mail


def probar_envio(para):
    """Email de prueba desde Parámetros. A diferencia del resto, si falla lanza el error para mostrarlo."""
    _armar([para], f"Prueba de email de {settings.EMPRESA_NOMBRE}",
           "Si recibiste este email, los avisos están funcionando.", titulo="Los avisos por email funcionan").send()


def estado_servidor():
    """Descripción del servidor de correo configurado en el .env, para mostrar en Parámetros."""
    backend = settings.EMAIL_BACKEND
    if 'console' in backend or 'locmem' in backend or 'dummy' in backend:
        return "Modo prueba: los emails no se envían, quedan escritos en el log del servidor."
    if 'smtp' in backend:
        return f"Servidor SMTP {settings.EMAIL_HOST or '(sin configurar)'}:{settings.EMAIL_PORT}."
    return backend


def enviar_email(para, asunto, texto, *, titulo=None, boton_url=None, boton_texto=None, ahora=False):
    """Email con versión HTML de marca. Por defecto sale al confirmarse la transacción en curso."""
    para = [p for p in para if p]
    if not para:
        return

    def _enviar():
        try:
            _armar(para, asunto, texto, titulo, boton_url, boton_texto).send()
        except Exception:
            logger.warning("No se pudo enviar el email '%s' a %s", asunto, para, exc_info=True)

    if ahora:
        _enviar()
    else:
        transaction.on_commit(_enviar)


def cliente(usuario, mensaje, *, asunto, detalle='', url=None, boton='Ver en Finanxis'):
    """Notificación en la app (la campana) + email al cliente."""
    Notificacion.objects.create(usuario=usuario, mensaje=mensaje)
    texto = f"Hola {usuario.first_name or usuario.username}:\n\n{mensaje}" + (f"\n\n{detalle}" if detalle else "")
    enviar_email([usuario.email], asunto, texto, titulo=mensaje, boton_url=url or url_absoluta('dashboard'),
                 boton_texto=boton)


def equipo(evento, asunto, texto, *, url=None, boton='Abrir en el panel'):
    enviar_email(destinatarios(evento), f"[{settings.EMPRESA_NOMBRE}] {asunto}", texto, titulo=asunto,
                 boton_url=url, boton_texto=boton)


def cliente_desc(u):
    nombre = u.get_full_name() or u.username
    return f"{nombre} ({u.username}{f', DNI {u.doc_nro}' if u.doc_nro else ''})"
