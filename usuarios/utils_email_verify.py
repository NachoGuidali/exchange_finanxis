from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.template.loader import render_to_string
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse


# DEMOOO


# utils_verificacion.py (o donde tenés send_verification_email)
import os
from datetime import datetime


def _ensure_log_dir(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass  # silencioso en demo

def save_verification_link_for_demo(user, link, request=None):
    """
    Guarda el enlace de verificación en un .log de texto si la bandera está activa.
    No hace nada en producción si DEMO_SAVE_VERIFICATION_LINK == False.
    """
    if not getattr(settings, "DEMO_SAVE_VERIFICATION_LINK", False):
        return

    log_path = str(getattr(settings, "VERIFICATION_LINK_LOG_FILE",
                           os.path.join(os.getcwd(), "verification_links.log")))
    _ensure_log_dir(log_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ip = ""
    try:
        if request:
            ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")) or ""
    except Exception:
        ip = ""

    line = (
        f"[{ts}] user_id={getattr(user, 'pk', '?')} "
        f"email={getattr(user, 'email', '')} "
        f"ip={ip} -> {link}\n"
    )

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # En demo no queremos romper el flujo si no se puede escribir
        pass





def build_verification_link(request, user):
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse('verify_email', kwargs={'uidb64': uidb64, 'token': token})
    # Absolute URL
    return request.build_absolute_uri(path)

def send_verification_email(request, user):
    link = build_verification_link(request, user)
    # 👇 Log de demo (no afecta producción si apagás la bandera)
    save_verification_link_for_demo(user, link, request)
    subject = "Confirmá tu email — Full Finanzas"
    body_txt = render_to_string('emails/verify_email.txt', {'user': user, 'link': link})
    body_html = render_to_string('emails/verify_email.html', {'user': user, 'link': link})
    send_mail(
        subject,
        body_txt,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        html_message=body_html,
    )

