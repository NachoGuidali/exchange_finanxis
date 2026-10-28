from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from .views import registro, dashboard, panel_admin, cambiar_estado_verificacion, agregar_saldo, panel_depositos, aprobar_deposito, historial_usuario, operar, enviar_retiro, aprobar_retiro, solicitar_retiro, exportar_historial_usuario, exportar_movimientos_admin, exportar_movimientos_usuario, obtener_notificaciones, contar_notificaciones, solicitar_retiro_cripto, aprobar_retiro_cripto, panel_retiros, rechazar_deposito_usdt, aprobar_deposito_usdt, panel_depositos_usdt, depositar_usdt, logout_view, verificar_boleto, comprobantes, descargar_boleto, home, actualizar_perfil, cambiar_email, cambiar_password, soporte, faq, perfil, configuracion, tyc, mis_movimientos, exportar_movimientos, mis_tickets, admin_usuario_perfil, rechazar_retiro_cripto, rechazar_retiro_ars, admin_usuarios_list, exchange_dashboard, exchange_export_csv, verify_email_notice, verify_email, resend_verification, change_email_form, change_email_submit, LoginViewCustom, guardar_config_exchange, rechazar_deposito, exportar_movimientos_usuario_admin
from . import views
from .views import geo as views_geo
from .views.twofa import activar_2fa, desactivar_2fa, login_2fa

urlpatterns = [
    path('', home, name='home'),
    path('login/', LoginViewCustom.as_view(), name='login'),
    path('login/2fa/', login_2fa, name='login_2fa'),
    path('registro/', registro, name='registro'),
    path('logout/', logout_view, name='logout'),
    path('dashboard/', dashboard, name='dashboard'),
    path('admin-dashboard/', panel_admin, name='panel_admin'),
    path('admin-dashboard/cambiar-estado/<int:user_id>/', cambiar_estado_verificacion, name='cambiar_estado_verificacion'),
    path('agregar-saldo/', agregar_saldo, name='agregar_saldo'),
    path('admin-dashboard/admin-depositos/', panel_depositos, name='panel_depositos'),
    path('admin-dashboard/aprobar-deposito/<int:deposito_id>/', aprobar_deposito, name='aprobar_deposito'),
    path('admin-dashboard/rechazar-deposito/<int:deposito_id>/', rechazar_deposito, name='rechazar_deposito'),
    path('historial-usuario/<int:user_id>/', historial_usuario, name='historial_usuario'),
    path('operar/', operar, name='operar'),
    path('admin-dashboard/retiro/aprobar/<int:id>/', aprobar_retiro, name='aprobar_retiro'),
    path('admin-dashboard/retiro/enviar/<int:id>/', enviar_retiro, name='enviar_retiro'),
    path('admin-dashboard/retiro/rechazar/<int:id>/', rechazar_retiro_ars, name='rechazar_retiro_ars'),

    path('solicitar-retiro/', solicitar_retiro, name='solicitar_retiro'),
    path('exportar-movimientos/', exportar_movimientos_usuario, name='exportar_movimientos_usuario'),
    path('admin-dashboard/exportar-movimientos/', exportar_movimientos_admin, name='exportar_movimientos_admin'),
    path('admin-dashboard/historial-usuario/<int:user_id>/exportar/', exportar_historial_usuario, name='exportar_historial_usuario'),
    path('notificaciones/ajax/', obtener_notificaciones, name='ajax_obtener_notificaciones'),
    path('notificaciones/contar/', contar_notificaciones, name='contar_notificaciones'),    
    path('solicitar-retiro-cripto/', solicitar_retiro_cripto, name='solicitar_retiro_cripto'),
    path('admin-dashboard/retiro-cripto/aprobar/<int:id>/', aprobar_retiro_cripto, name='aprobar_retiro_cripto'),
    path('admin-dashboard/retiro-cripto/rechazar/<int:id>/', rechazar_retiro_cripto, name='rechazar_retiro_cripto'),
    path('admin-dashboard/retiros/', panel_retiros, name='panel_retiros'),
    path('depositar-usdt/', depositar_usdt, name='depositar_usdt'),
    path('depositar-dolares/', views.depositar_usd, name='depositar_usd'),
    path('admin-dashboard/depositos-usdt/', panel_depositos_usdt, name='panel_depositos_usdt'),
    path('admin-dashboard/depositos-usdt/<int:deposito_id>/aprobar/', aprobar_deposito_usdt, name='aprobar_deposito_usdt'),
    path('admin-dashboard/depositos-usdt/<int:deposito_id>/rechazar/', rechazar_deposito_usdt, name='rechazar_deposito_usdt'),
    path("admin-dashboard/usuario/<int:user_id>/", admin_usuario_perfil, name="admin_usuario_perfil"),
    path("admin-dashboard/usuarios/", admin_usuarios_list, name="admin_usuarios_list"),
    
    path('admin-dashboard/exchange/', exchange_dashboard, name='exchange_dashboard'),
    path('admin-dashboard/exchange/export.csv', exchange_export_csv, name='exchange_export_csv'),
    
    path("comprobantes/", comprobantes, name="comprobantes"),
    path("comprobantes/<str:numero>/", views.ver_comprobante, name="ver_comprobante"),
    path("boletos/<str:numero>/", verificar_boleto, name="verificar_boleto"),
    path("boletos/<str:numero>/descargar/", descargar_boleto, name="descargar_boleto"),
    path('perfil/', perfil, name='perfil'),
    path('configuracion/', configuracion, name='configuracion'),

    # Acciones de configuración
    path('configuracion/perfil/actualizar/', actualizar_perfil, name='actualizar_perfil'),
    path('configuracion/seguridad/password/', cambiar_password, name='cambiar_password'),
    path('configuracion/seguridad/email/', cambiar_email, name='cambiar_email'),
    path('configuracion/seguridad/2fa/activar/', activar_2fa, name='activar_2fa'),
    path('configuracion/seguridad/2fa/desactivar/', desactivar_2fa, name='desactivar_2fa'),

    # Soporte / Ayuda
    path('soporte/', soporte, name='soporte'),
    path('faq/', faq, name='faq'),
    path("api/geo/paises/",      views_geo.geo_paises,      name="geo_paises"),
    path("api/geo/provincias/",  views_geo.geo_provincias,  name="geo_provincias"),
    path("api/geo/localidades/", views_geo.geo_localidades, name="geo_localidades"),
    path("tyc/", tyc, name="tyc"),


    path("movimientos/", mis_movimientos, name="mis_movimientos"),
    path("movimientos/exportar/", exportar_movimientos, name="movimientos_exportar"),

    path("soporte/mis-tickets/", mis_tickets, name="mis_tickets"),
    #confirmacion email
    path('auth/verify-email/', verify_email_notice, name='verify_email_notice'),
    path('auth/verify/<uidb64>/<token>/', verify_email, name='verify_email'),
    path('auth/resend-verification/', resend_verification, name='resend_verification'),
    path('auth/change-email/', change_email_form, name='change_email_form'),
    path('auth/change-email/submit/', change_email_submit, name='change_email_submit'),

    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="usuarios/password_reset_form.html",
            email_template_name="usuarios/password_reset_email.txt",
            subject_template_name="usuarios/password_reset_subject.txt",
            success_url=reverse_lazy("password_reset_done"),
            from_email=None,  # usa DEFAULT_FROM_EMAIL de settings
        ),
        name="password_reset",
    ),
    # Aviso “te enviamos el mail”
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="usuarios/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    # Link del mail (con token)
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="usuarios/password_reset_confirm.html",
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    # Confirmación de contraseña cambiada
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="usuarios/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
    # path('admin-dashboard/config/', exchange_config_view, name='exchange_config'),
    path('admin-dashboard/parametros/', views.parametros_exchange, name='parametros_exchange'),
    path('admin-dashboard/movimientos/', views.admin_movimientos, name='admin_movimientos'),
    path('admin-dashboard/cotizaciones/', views.admin_cotizaciones, name='admin_cotizaciones'),
    path('admin-dashboard/cotizaciones/actualizar/', views.actualizar_cotizaciones_admin, name='actualizar_cotizaciones_admin'),
    path('admin-dashboard/cotizaciones/manual/', views.fijar_precio_manual, name='fijar_precio_manual'),
    path('admin-dashboard/cotizaciones/manual/quitar/', views.quitar_precio_manual, name='quitar_precio_manual'),
    path('admin-dashboard/config/save/', guardar_config_exchange, name='guardar_config_exchange'),
    path('admin-dashboard/depositos-usdt/<int:deposito_id>/reverificar/', views.reverificar_deposito_usdt, name='reverificar_deposito_usdt'),
    path('admin-dashboard/compliance/', views.compliance_alertas, name='compliance_alertas'),
    path('admin-dashboard/compliance/<int:alerta_id>/resolver/', views.resolver_alerta, name='resolver_alerta'),
    path('admin-dashboard/auditoria/', views.auditoria, name='auditoria'),
    path('operar/confirmar/', views.confirmar_operacion, name='confirmar_operacion'),
    path(
        'admin-dashboard/usuarios/<int:user_id>/exportar-movimientos/',
        exportar_movimientos_usuario_admin,
        name='exportar_movimientos_usuario_admin'
    ),


]
