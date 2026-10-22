import base64
import io
import time

import pyotp
import qrcode
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

User = get_user_model()

EMISOR = "Finanxis"
PENDIENTE_TTL = 5 * 60   # segundos para ingresar el código tras la contraseña
MAX_INTENTOS = 5


def codigo_valido(secret, codigo):
    codigo = (codigo or '').strip().replace(' ', '')
    return bool(secret) and codigo.isdigit() and pyotp.TOTP(secret).verify(codigo, valid_window=1)


def iniciar_login_2fa(request, user, next_url):
    """Llamado por el login cuando la contraseña es correcta y el usuario tiene 2FA."""
    request.session['2fa_pendiente'] = {
        'user_id': user.pk,
        'backend': user.backend,
        'next': next_url,
        'desde': time.time(),
        'intentos': 0,
    }
    return redirect('login_2fa')


def login_2fa(request):
    pendiente = request.session.get('2fa_pendiente')
    if not pendiente or time.time() - pendiente['desde'] > PENDIENTE_TTL:
        request.session.pop('2fa_pendiente', None)
        messages.error(request, "La verificación expiró. Ingresá de nuevo.")
        return redirect('login')

    if request.method == 'POST':
        user = User.objects.filter(pk=pendiente['user_id'], is_active=True).first()
        if user and codigo_valido(user.totp_secret, request.POST.get('codigo')):
            del request.session['2fa_pendiente']
            login(request, user, backend=pendiente['backend'])
            next_url = pendiente.get('next') or ''
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
                next_url = reverse('dashboard')
            return redirect(next_url)

        pendiente['intentos'] += 1
        if pendiente['intentos'] >= MAX_INTENTOS:
            request.session.pop('2fa_pendiente', None)
            messages.error(request, "Demasiados intentos. Ingresá de nuevo.")
            return redirect('login')
        request.session['2fa_pendiente'] = pendiente
        messages.error(request, "Código incorrecto.")

    return render(request, 'usuarios/login_2fa.html')


def _qr_data_uri(texto):
    buf = io.BytesIO()
    qrcode.make(texto, box_size=6, border=2).save(buf, format='PNG')
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode('ascii')


@login_required
def activar_2fa(request):
    u = request.user
    if u.totp_activo:
        messages.info(request, "2FA ya está activo.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    # El secreto vive en la sesión hasta que el usuario demuestre que lo cargó en su app
    secret = request.session.get('2fa_secret_nuevo')
    if not secret:
        secret = pyotp.random_base32()
        request.session['2fa_secret_nuevo'] = secret

    if request.method == 'POST':
        if codigo_valido(secret, request.POST.get('codigo')):
            u.totp_secret = secret
            u.totp_activo = True
            u.save(update_fields=['totp_secret', 'totp_activo'])
            del request.session['2fa_secret_nuevo']
            messages.success(request, "2FA activado. Desde ahora te vamos a pedir el código al ingresar.")
            return redirect(f"{reverse('configuracion')}?tab=seguridad")
        messages.error(request, "Código incorrecto. Revisá la hora de tu teléfono y probá de nuevo.")

    uri = pyotp.TOTP(secret).provisioning_uri(name=u.email or u.username, issuer_name=EMISOR)
    return render(request, 'usuarios/activar_2fa.html', {'qr': _qr_data_uri(uri), 'secret': secret})


@login_required
@require_POST
def desactivar_2fa(request):
    u = request.user
    if not codigo_valido(u.totp_secret, request.POST.get('codigo')):
        messages.error(request, "Código incorrecto: 2FA sigue activo.")
    else:
        u.totp_secret = ''
        u.totp_activo = False
        u.save(update_fields=['totp_secret', 'totp_activo'])
        messages.success(request, "2FA desactivado.")
    return redirect(f"{reverse('configuracion')}?tab=seguridad")
