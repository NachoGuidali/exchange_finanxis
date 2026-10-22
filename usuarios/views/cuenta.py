"""Perfil, configuración, soporte y notificaciones del usuario."""
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import SupportTicketForm
from ..models import Localidad, Notificacion, Pais, Provincia, SupportTicket
from ..utils_email_verify import send_verification_email

User = get_user_model()


@login_required
def perfil(request):
    """Landing de Perfil (lectura)."""
    return render(request, 'usuarios/perfil.html')


@login_required
def configuracion(request):
    """Configuración con tabs (Perfil / Seguridad / Apariencia / Ayuda)."""
    return render(request, 'usuarios/configuracion.html')


@require_POST
@login_required
def actualizar_perfil(request):
    """
    Actualiza datos de perfil PERMITIDOS (NO identidad).
    Inmutables: first_name, last_name, doc_tipo, doc_nro
    Permitidos: telefono, domicilio (legacy), y domicilio estructurado,
                nacionalidad, fecha_nacimiento, lugar_nacimiento,
                persona_tipo, estado_civil, sexo, código postal, etc.
    """

    u = request.user

    # --- Detectar intento de cambiar identidad (se ignora) ---
    tried_identity = False
    if "first_name" in request.POST and (request.POST.get("first_name","").strip() != u.first_name):
        tried_identity = True
    if "last_name" in request.POST and (request.POST.get("last_name","").strip() != u.last_name):
        tried_identity = True
    if "doc_tipo" in request.POST and (request.POST.get("doc_tipo","").strip().upper() != (u.doc_tipo or "")):
        tried_identity = True
    if "doc_nro" in request.POST and (request.POST.get("doc_nro","").strip() != (u.doc_nro or "")):
        tried_identity = True
    # Campo combinado "doc" (tipo + nro) también se ignora:
    if "doc" in request.POST and request.POST.get("doc","").strip():
        tried_identity = True

    # --- Campos permitidos ---
    telefono       = (request.POST.get('telefono')       or '').strip()
    domicilio      = (request.POST.get('domicilio')      or '').strip()  # legacy (opcional)
    nacionalidad   = (request.POST.get('nacionalidad')   or '').strip()
    lugar_nac      = (request.POST.get('lugar_nacimiento') or '').strip()
    persona_tipo   = (request.POST.get('persona_tipo')   or '').strip()  # "FISICA"/"JURIDICA"
    estado_civil   = (request.POST.get('estado_civil')   or '').strip()
    sexo           = (request.POST.get('sexo')           or '').strip()

    # Fecha (YYYY-MM-DD)
    fecha_nac = None
    raw_fecha = (request.POST.get('fecha_nacimiento') or '').strip()
    if raw_fecha:
        try:
            fecha_nac = datetime.strptime(raw_fecha, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Fecha de nacimiento inválida. Usá formato AAAA-MM-DD.")
            return redirect(f"{reverse('configuracion')}?tab=perfil")

    # Domicilio estructurado
    pais_id        = request.POST.get('pais') or None
    provincia_id   = request.POST.get('provincia') or None
    localidad_id   = request.POST.get('localidad') or None
    codigo_postal  = (request.POST.get('codigo_postal') or '').strip()
    calle          = (request.POST.get('calle') or '').strip()
    numero_calle   = (request.POST.get('numero_calle') or '').strip()
    piso           = (request.POST.get('piso') or '').strip()
    depto          = (request.POST.get('depto') or '').strip()

    # Resolver FKs (opcionales)
    pais_obj = prov_obj = loc_obj = None
    try:
        if pais_id:
            pais_obj = Pais.objects.get(pk=pais_id)
        if provincia_id:
            prov_obj = Provincia.objects.get(pk=provincia_id)
        if localidad_id:
            loc_obj = Localidad.objects.get(pk=localidad_id)
    except (Pais.DoesNotExist, Provincia.DoesNotExist, Localidad.DoesNotExist):
        messages.error(request, "Selección de país/provincia/localidad inválida.")
        return redirect(f"{reverse('configuracion')}?tab=perfil")

    try:
        with transaction.atomic():
            # Asignar SOLO campos permitidos
            u.telefono         = telefono
            u.domicilio        = domicilio  # si todavía lo usás en algún lado
            u.nacionalidad     = nacionalidad
            u.lugar_nacimiento = lugar_nac
            u.persona_tipo     = persona_tipo or u.persona_tipo
            u.estado_civil     = estado_civil or u.estado_civil
            u.sexo             = sexo or u.sexo

            u.fecha_nacimiento = fecha_nac if raw_fecha else u.fecha_nacimiento

            u.pais        = pais_obj
            u.provincia   = prov_obj
            u.localidad   = loc_obj
            u.codigo_postal = codigo_postal
            u.calle         = calle
            u.numero_calle  = numero_calle
            u.piso          = piso
            u.depto         = depto

            # ¡NO tocar identidad!
            # u.first_name, u.last_name, u.doc_tipo, u.doc_nro -> se dejan igual

            u.save()

        if tried_identity:
            messages.info(request, "Nombre/Apellido y Documento no se pueden modificar; se mantuvieron sin cambios.")
        messages.success(request, "Perfil actualizado.")
    except Exception as e:
        messages.error(request, f"Ocurrió un error al actualizar el perfil: {e}")

    return redirect(f"{reverse('configuracion')}?tab=perfil")


@require_POST
@login_required
def cambiar_password(request):
    """
    Cambia la contraseña del usuario.
    Campos: actual, nueva, confirmar
    """
    u = request.user
    actual    = request.POST.get('actual', '')
    nueva     = request.POST.get('nueva', '')
    confirmar = request.POST.get('confirmar', '')

    if not u.check_password(actual):
        messages.error(request, "La contraseña actual no es correcta.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    if nueva != confirmar:
        messages.error(request, "La nueva contraseña y su confirmación no coinciden.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    if len(nueva) < 8:
        messages.error(request, "La nueva contraseña debe tener al menos 8 caracteres.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    try:
        u.set_password(nueva)
        u.save()
        # Mantener la sesión iniciada tras el cambio
        update_session_auth_hash(request, u)
        messages.success(request, "Contraseña actualizada correctamente.")
    except Exception as e:
        messages.error(request, f"No se pudo actualizar la contraseña: {e}")

    return redirect(f"{reverse('configuracion')}?tab=seguridad")


@require_POST
@login_required
def cambiar_email(request):
    """
    Cambia el email del usuario (pide contraseña actual).
    Campos: email, password
    """
    u = request.user
    email    = (request.POST.get('email') or '').strip().lower()
    password = request.POST.get('password') or ''

    if not u.check_password(password):
        messages.error(request, "La contraseña ingresada es incorrecta.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Ingresá un email válido.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    # Evitar duplicados de email en el sistema
    if User.objects.filter(email__iexact=email).exclude(pk=u.pk).exists():
        messages.error(request, "Ese email ya está en uso por otro usuario.")
        return redirect(f"{reverse('configuracion')}?tab=seguridad")

    u.email = email
    u.email_confirmed = False
    u.email_confirmed_at = None
    u.email_confirm_sent_at = timezone.now()
    u.save(update_fields=['email', 'email_confirmed', 'email_confirmed_at', 'email_confirm_sent_at'])
    send_verification_email(request, u)
    messages.success(request, "Email actualizado. Te enviamos un enlace para verificarlo.")
    return redirect('verify_email_notice')


@login_required
def soporte(request):
    if request.method == "POST":
        form = SupportTicketForm(request.POST, request.FILES)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.usuario = request.user
            ticket.save()
            try:
                send_mail(
                    subject=f"[Full Finanzas] Nuevo ticket #{ticket.id}: {ticket.asunto}",
                    message=(
                        f"Usuario: {request.user.username} ({ticket.email})\n"
                        f"Categoría: {ticket.categoria}\n"
                        f"Prioridad: {ticket.prioridad}\n"
                        f"Mensaje:\n{ticket.mensaje}\n"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=getattr(settings, "SOPORTE_TO", []),
                    fail_silently=True,
                )
            except Exception:
                pass  # no interrumpimos UX si el mail falla

            # Confirmación al usuario (opcional)
            try:
                send_mail(
                    subject=f"Recibimos tu ticket #{ticket.id}",
                    message=(
                        f"Hola {request.user.username},\n\n"
                        f"Recibimos tu solicitud y ya la estamos revisando.\n"
                        f"Asunto: {ticket.asunto}\n"
                        f"ID: #{ticket.id}\n\n"
                        f"Te responderemos a este email.\n\n"
                        f"— Equipo de Soporte Full Finanzas"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[ticket.email],
                    fail_silently=True,
                )
            except Exception:
                pass
            messages.success(request, "Tu ticket fue creado correctamente. ¡Te vamos a escribir por email!")
            return redirect("soporte")
        else:
            messages.error(request, "Revisá los campos resaltados.")
    else:
        form = SupportTicketForm(initial={"email": request.user.email})
    return render(request, "usuarios/soporte.html", {"form": form})


@login_required
def mis_tickets(request):
    qs = SupportTicket.objects.filter(usuario=request.user).order_by("-creado_en")
    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "usuarios/mis_tickets.html", {"page_obj": page_obj})


@login_required
def obtener_notificaciones(request):
    queryset = Notificacion.objects.filter(usuario=request.user).order_by('-fecha')
    notificaciones = list(queryset[:10])  # slicing primero

    # marcar como leídas solo esas
    Notificacion.objects.filter(id__in=[n.id for n in notificaciones]).update(leida=True)

    data = [
        {
            'mensaje': n.mensaje,
            'fecha': n.fecha.strftime('%d/%m/%Y %H:%M'),
        }
        for n in notificaciones
    ]
    return JsonResponse({'notificaciones': data})


@login_required
def contar_notificaciones(request):
    cantidad = Notificacion.objects.filter(usuario=request.user, leida=False).count()
    return JsonResponse({'no_leidas': cantidad})
