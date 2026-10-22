from django.contrib import admin
from django.utils.html import format_html
from django.contrib.auth.admin import UserAdmin
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils import timezone  # 👈 NUEVO
from django.contrib import messages

from .models import Usuario, Cotizacion, RetiroARS, Pais, Provincia, Localidad, Asiento, LineaAsiento
from .services import fondos
from .services.comun import OperacionError


@admin.register(Usuario)
class CustomUserAdmin(UserAdmin):
    model = Usuario

    # -------------------------
    # LISTA
    # -------------------------
    list_display = (
        'username',
        'email',
        'email_confirmed',       # 👈 NUEVO
        'estado_verificacion',
        'nombre',
        'saldo_ars',
        'saldo_usdt',
        'saldo_usd',
        'date_joined',
        'ver_historial',
    )
    list_display_links = ('username', 'email')
    list_filter = (
        'estado_verificacion',
        'email_confirmed',       # 👈 NUEVO
        'is_active',
        'is_staff',
        'is_superuser',
        'date_joined',
    )
    search_fields = (
        'username',
        'email',
        'first_name',
        'last_name',
        'doc_nro',
        'telefono',
    )
    ordering = ('-date_joined',)
    date_hierarchy = 'date_joined'
    actions = [
        'aprobar_verificacion',
        'rechazar_verificacion',
        'marcar_email_verificado',   # 👈 NUEVA ACCIÓN
    ]

    # -------------------------
    # CAMPOS SOLO LECTURA
    # -------------------------
    readonly_fields = (
        'dni_frente_preview',
        'dni_dorso_preview',
        'saldo_ars',
        'saldo_usdt',
        'saldo_usd',
        'last_login',
        'date_joined',
        'email_confirmed_at',        # 👈 NUEVO
        'email_confirm_sent_at',     # 👈 NUEVO
    )

    # -------------------------
    # FORM DE EDICIÓN
    # -------------------------
    fieldsets = UserAdmin.fieldsets + (
        ("KYC", {
            "fields": (
                "doc_tipo", "doc_nro", "nacionalidad", "telefono",
                "dni_frente", "dni_dorso", "estado_verificacion"
            )
        }),
        ("Domicilio", {
            "fields": ("pais", "provincia", "localidad", "domicilio")
        }),
        ("Email / Verificación", {   # 👈 NUEVO BLOQUE
            "fields": (
                "email_confirmed",
                "email_confirmed_at",
                "email_confirm_sent_at",
            )
        }),
        ("Saldos", {
            "fields": ("saldo_ars", "saldo_usdt", "saldo_usd")
        }),
    )

    # (Opcional) si creás usuarios desde admin y querés capturar KYC en el alta:
    add_fieldsets = UserAdmin.add_fieldsets + (
        (_('KYC / Identidad'), {
            'classes': ('wide',),
            'fields': ('doc_tipo', 'doc_nro', 'nacionalidad', 'domicilio', 'telefono'),
        }),
    )

    # -------------------------
    # MÉTODOS AUX
    # -------------------------
    @admin.display(description='Nombre')
    def nombre(self, obj: Usuario):
        full = f"{obj.first_name} {obj.last_name}".strip()
        return full or obj.username

    @admin.display(description='DNI frente')
    def dni_frente_preview(self, obj: Usuario):
        if obj.dni_frente:
            url = obj.dni_frente.url
            return format_html(
                '<a href="{}" target="_blank"><img src="{}" style="height:80px;border:1px solid #ddd;border-radius:6px"/></a>',
                url, url
            )
        return "—"

    @admin.display(description='DNI dorso')
    def dni_dorso_preview(self, obj: Usuario):
        if obj.dni_dorso:
            url = obj.dni_dorso.url
            return format_html(
                '<a href="{}" target="_blank"><img src="{}" style="height:80px;border:1px solid #ddd;border-radius:6px"/></a>',
                url, url
            )
        return "—"

    @admin.display(description='Historial')
    def ver_historial(self, obj: Usuario):
        try:
            url = reverse('historial_usuario', args=[obj.id])
            return format_html('<a class="button" href="{}" target="_blank">Ver</a>', url)
        except Exception:
            return "—"

    # -------------------------
    # ACCIONES
    # -------------------------
    @admin.action(description="Aprobar verificación (activar cuenta)")
    def aprobar_verificacion(self, request, queryset):
        updated = 0
        for u in queryset:
            u.estado_verificacion = 'aprobado'
            u.is_active = True   # el usuario puede loguearse y operar
            u.save(update_fields=['estado_verificacion', 'is_active'])
            updated += 1
        self.message_user(request, f"{updated} usuario(s) aprobados.")

    @admin.action(description="Rechazar verificación (mantener cuenta activa)")
    def rechazar_verificacion(self, request, queryset):
        updated = 0
        for u in queryset:
            u.estado_verificacion = 'rechazado'
            # mantenemos is_active=True para que vea la pantalla de “no verificado”
            u.save(update_fields=['estado_verificacion'])
            updated += 1
        self.message_user(request, f"{updated} usuario(s) marcados como rechazados.")

    @admin.action(description="Marcar email como verificado")  # 👈 NUEVA ACCIÓN
    def marcar_email_verificado(self, request, queryset):
        ahora = timezone.now()
        updated = 0
        for u in queryset:
            if not u.email_confirmed:
                u.email_confirmed = True
                u.email_confirmed_at = ahora
                u.save(update_fields=['email_confirmed', 'email_confirmed_at'])
                updated += 1
        self.message_user(request, f"{updated} usuario(s) marcados con email verificado.")


@admin.register(Cotizacion)
class CotizacionAdmin(admin.ModelAdmin):
    list_display = ('moneda', 'compra', 'venta', 'fecha')
    list_filter = ('moneda',)
    ordering = ('-fecha',)


@admin.register(RetiroARS)
class RetiroARSAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'monto', 'alias', 'cbu', 'estado', 'fecha_solicitud')
    list_filter = ('estado',)
    actions = ['aprobar_retiros', 'marcar_como_enviado']
    # el estado y el monto solo cambian vía services/fondos (mayor, boleto y notificación)
    readonly_fields = ('usuario', 'monto', 'estado', 'fecha_solicitud', 'fecha_actualizacion')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _aplicar(self, request, queryset, accion):
        ok = 0
        for retiro in queryset:
            try:
                accion(retiro)
                ok += 1
            except OperacionError as e:
                self.message_user(request, f"Retiro #{retiro.pk}: {e}", messages.WARNING)
        self.message_user(request, f"{ok} retiro(s) procesados.")

    @admin.action(description="Aprobar retiros seleccionados")
    def aprobar_retiros(self, request, queryset):
        self._aplicar(request, queryset, lambda r: fondos.aprobar_retiro_ars(r.pk))

    @admin.action(description="Marcar como enviados")
    def marcar_como_enviado(self, request, queryset):
        self._aplicar(request, queryset, lambda r: fondos.enviar_retiro_ars(r.pk, request.user))


class LineaAsientoInline(admin.TabularInline):
    model = LineaAsiento
    fields = ('cuenta', 'moneda', 'monto')
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Asiento)
class AsientoAdmin(admin.ModelAdmin):
    """Solo lectura: el mayor no se edita, se corrige con un asiento de ajuste."""
    list_display = ('id', 'fecha', 'tipo', 'descripcion', 'referencia', 'creado_por')
    list_filter = ('tipo',)
    search_fields = ('descripcion', 'referencia')
    inlines = [LineaAsientoInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Pais)
admin.site.register(Provincia)
admin.site.register(Localidad)
