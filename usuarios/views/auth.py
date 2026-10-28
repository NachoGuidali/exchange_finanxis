"""Registro, login (con 2FA), logout y verificación de email."""

from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.http import require_POST

from ..forms import EmailOrUsernameAuthenticationForm, RegistroUsuarioForm
from ..utils_email_verify import send_verification_email
from .twofa import iniciar_login_2fa
from ..limites import limitar

User = get_user_model()


class LoginViewCustom(LoginView):
    authentication_form = EmailOrUsernameAuthenticationForm
    template_name = 'usuarios/login.html'

    def form_valid(self, form):
        user = form.get_user()
        # ModelBackend ya bloquea is_active=False, pero podés dar feedback:
        if not user.is_active:
            messages.error(self.request, "Tu email no está verificado. Revisá tu bandeja o reenviá el correo.")
            return self.form_invalid(form)
        if user.totp_activo:
            # contraseña OK: falta el segundo factor antes de iniciar sesión
            return iniciar_login_2fa(self.request, user, self.get_success_url())
        return super().form_valid(form)


def login_bloqueado(request, credentials, *args, **kwargs):
    """Respuesta de django-axes cuando se superan los intentos fallidos de login."""
    messages.error(request, "Demasiados intentos fallidos. Esperá una hora o restablecé tu contraseña.")
    return render(request, 'usuarios/login.html', {'form': EmailOrUsernameAuthenticationForm(request)}, status=429)

@limitar('registro', 5, 3600)
def registro(request):
    if request.method == 'POST':
        form = RegistroUsuarioForm(request.POST, request.FILES)
        if form.is_valid():
            # 1) Crear instancia sin guardar para poder setear flags
            user = form.save(commit=False)

            # 2) Bloquear login hasta verificar email
            user.is_active = True

            # (Opcional, si usás KYC y estados propios)
            user.estado_verificacion = 'pendiente'

            # 3) Timestamp de cuándo enviamos el correo (útil para rate-limit reenvíos)
            user.email_confirm_sent_at = timezone.now()

            # 4) Guardar definitivamente
            user.save()

            # 5) Enviar email de verificación (PASAR LA INSTANCIA)
            send_verification_email(request, user)

            # 6) Aviso + redirección a pantalla “revisá tu correo”
            messages.success(
                request,
                'Registro creado. Te enviamos un correo para verificar tu email.'
            )
            return redirect('verify_email_notice')
    else:
        form = RegistroUsuarioForm()

    return render(request, 'usuarios/registro.html', {'form': form})


@login_required
@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada correctamente.")
    return redirect('login')


@login_required
def verify_email_notice(request):
    # Página que dice "revisá tu email" + botón "reenviar" + "cambiar email"
    if request.user.email_confirmed:
        return redirect('dashboard')  # ya confirmado
    return render(request, 'usuarios/verify_email_notice.html')


@limitar('reenvio_verificacion', 3, 3600, por='usuario')
@login_required
@require_POST
def resend_verification(request):
    if request.user.email_confirmed:
        return redirect('dashboard')
    send_verification_email(request, request.user)
    request.user.email_confirm_sent_at = timezone.now()
    request.user.save(update_fields=['email_confirm_sent_at'])
    messages.success(request, "Te enviamos un nuevo email de verificación.")
    return redirect('verify_email_notice')


@login_required
def change_email_form(request):
    if request.user.email_confirmed:
        return redirect('dashboard')
    return render(request, 'usuarios/change_email_form.html')


@limitar('cambio_email', 5, 3600, por='usuario')
@login_required
@require_POST
def change_email_submit(request):
    if request.user.email_confirmed:
        return redirect('dashboard')

    new_email = (request.POST.get('email') or '').strip().lower()
    try:
        validate_email(new_email)
    except ValidationError:
        messages.error(request, "Ingresá un email válido.")
        return redirect('change_email_form')

    if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
        messages.error(request, "Ese email ya está en uso.")
        return redirect('change_email_form')

    request.user.email = new_email
    request.user.email_confirmed = False
    request.user.save(update_fields=['email', 'email_confirmed'])

    send_verification_email(request, request.user)
    messages.success(request, "Actualizamos tu email. Te enviamos un nuevo enlace de verificación.")
    return redirect('verify_email_notice')


def verify_email(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid)
    except Exception:
        user = None

    if user and default_token_generator.check_token(user, token):
        if not user.email_confirmed:
            user.email_confirmed = True
            user.email_confirmed_at = timezone.now()
            user.is_active = True
            user.save(update_fields=['email_confirmed', 'email_confirmed_at'])
        messages.success(request, "¡Email verificado con éxito!")
        # Si está logueado va al dashboard; si no, al login
        if request.user.is_authenticated:
            return redirect('dashboard')
        return redirect('login')
    else:
        messages.error(request, "Enlace inválido o expirado. Pedí uno nuevo.")
        if request.user.is_authenticated:
            return redirect('verify_email_notice')
        return redirect('login')
