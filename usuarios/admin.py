from django.contrib import admin
from django.utils.html import format_html
from django.contrib.auth.admin import UserAdmin
from .models import Usuario, Cotizacion, RetiroARS
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


# Register your models here.

@admin.register(Usuario)
class CustomUserAdmin(UserAdmin):
    model = Usuario

    # -------------------------
    # LISTA
    # -------------------------
    list_display = (
        'username', 'email', 'nombre', 'estado_verificacion',
        'saldo_ars', 'saldo_usdt', 'saldo_usd', 'date_joined', 'ver_historial'
    )
    list_display_links = ('username', 'email')
    list_filter = ('estado_verificacion', 'is_active', 'is_staff', 'is_superuser', 'date_joined')
    search_fields = ('username', 'email', 'first_name', 'last_name', 'doc_nro', 'telefono')
    ordering = ('-date_joined',)
    date_hierarchy = 'date_joined'
    actions = ['aprobar_verificacion', 'rechazar_verificacion']

    # -------------------------
    # CAMPOS SOLO LECTURA (evita tocar saldos a mano)
    # -------------------------
    readonly_fields = (
        'dni_frente_preview', 'dni_dorso_preview',
        'saldo_ars', 'saldo_usdt', 'saldo_usd',
        'last_login', 'date_joined',
    )

    # -------------------------
    # FORM DE EDICIÓN
    # -------------------------
    fieldsets = UserAdmin.fieldsets + (
        (_('KYC / Identidad'), {
            'fields': ('doc_tipo', 'doc_nro', 'nacionalidad', 'domicilio', 'telefono')
        }),
        (_('Documentos'), {
            'fields': ('dni_frente_preview', 'dni_frente', 'dni_dorso_preview', 'dni_dorso')
        }),
        (_('Verificación'), {
            'fields': ('estado_verificacion',)
        }),
        (_('Saldos'), {
            'fields': ('saldo_ars', 'saldo_usdt', 'saldo_usd')
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
            return format_html('<a href="{}" target="_blank"><img src="{}" style="height:80px;border:1px solid #ddd;border-radius:6px"/></a>', url, url)
        return "—"

    @admin.display(description='DNI dorso')
    def dni_dorso_preview(self, obj: Usuario):
        if obj.dni_dorso:
            url = obj.dni_dorso.url
            return format_html('<a href="{}" target="_blank"><img src="{}" style="height:80px;border:1px solid #ddd;border-radius:6px"/></a>', url, url)
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

    def aprobar_retiros(self, request, queryset):
        for retiro in queryset.filter(estado='pendiente'):
            retiro.estado = 'aprobado'
            retiro.save()
    aprobar_retiros.short_description = "Aprobar retiros seleccionados"

    def marcar_como_enviado(self, request, queryset):
        for retiro in queryset.filter(estado='aprobado'):
            retiro.estado = 'enviado'
            retiro.save()
    marcar_como_enviado.short_description = "Marcar como enviados"