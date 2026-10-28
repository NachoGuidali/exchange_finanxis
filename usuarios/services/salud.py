"""
Chequeos de "Salud del sitio": que la infraestructura responda, que los procesos automáticos
estén al día y que la configuración de producción esté completa. Cada chequeo devuelve
un estado ('ok', 'aviso' o 'error'), un título y el detalle en lenguaje simple.
"""
import os
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from usuarios.models import (
    BoletoOperacion,
    DepositoARS,
    DepositoUSDT,
    ErrorRegistrado,
    ExchangeConfig,
    RetiroARS,
    RetiroCrypto,
    Usuario,
)


def _c(estado, titulo, detalle):
    return {'estado': estado, 'titulo': titulo, 'detalle': detalle}


def infraestructura():
    chequeos = []

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        chequeos.append(_c('ok', 'Base de datos', f"Responde ({connection.vendor})."))
    except Exception as e:
        chequeos.append(_c('error', 'Base de datos', f"No responde: {e}"))

    try:
        cache.set('salud:prueba', 'ok', 10)
        ok = cache.get('salud:prueba') == 'ok'
        backend = settings.CACHES['default']['BACKEND'].rsplit('.', 1)[-1]
        chequeos.append(_c('ok' if ok else 'error', 'Cache', f"{'Responde' if ok else 'No guarda datos'} ({backend})."))
    except Exception as e:
        chequeos.append(_c('error', 'Cache', f"No responde: {e}"))

    chequeos.append(_tareas())

    media = settings.MEDIA_ROOT
    escribible = os.path.isdir(media) and os.access(media, os.W_OK)
    chequeos.append(_c('ok' if escribible else 'error', 'Archivos subidos',
                       "La carpeta de archivos se puede escribir." if escribible
                       else f"No se puede escribir en {media}: fallarían DNI, comprobantes y boletos."))

    try:
        from weasyprint import HTML  # noqa: F401
        pdf = _c('ok', 'Comprobantes PDF', "WeasyPrint está disponible.")
    except Exception:
        pdf = _c('aviso' if settings.BOLETOS_TOLERAR_ERROR_PDF else 'error', 'Comprobantes PDF',
                 "WeasyPrint no está disponible: los comprobantes se guardan sin PDF (solo imagen)."
                 if settings.BOLETOS_TOLERAR_ERROR_PDF else
                 "WeasyPrint no está disponible: las operaciones fallan al emitir el comprobante.")
    sin_pdf = BoletoOperacion.objects.filter(fecha_emision__gte=timezone.now() - timedelta(days=7), pdf='').count()
    if sin_pdf:
        pdf['detalle'] += f" {sin_pdf} comprobante{'s' if sin_pdf != 1 else ''} sin PDF en los últimos 7 días."
    chequeos.append(pdf)
    return chequeos


def _tareas():
    if settings.COTIZACION_ACTUALIZAR_AL_VUELO:
        return _c('aviso', 'Tareas automáticas (Celery)',
                  "No se usan en este servidor: la cotización se actualiza al entrar a Operar y la "
                  "verificación de depósitos USDT y la conciliación diaria no corren solas.")
    try:
        from core_exchange.celery import app
        # si el broker (Redis) no responde, cortar rápido en lugar de reintentar
        with app.connection_for_write() as conexion:
            conexion.ensure_connection(max_retries=1, timeout=2)
        respuestas = app.control.ping(timeout=1.0) or []
    except Exception as e:
        return _c('error', 'Tareas automáticas (Celery)', f"No se pudo conectar con el broker de tareas: {e}")
    if respuestas:
        return _c('ok', 'Tareas automáticas (Celery)', f"{len(respuestas)} worker{'s' if len(respuestas) != 1 else ''} respondiendo.")
    return _c('error', 'Tareas automáticas (Celery)',
              "Ningún worker responde: no se actualizan cotizaciones ni se verifican depósitos USDT.")


def procesos():
    from usuarios.services.cotizaciones import FUENTES, ultima
    from usuarios.services.ledger import diferencias_de_conciliacion
    from usuarios.utils import cotizacion_vigente

    chequeos = []
    for moneda in FUENTES:
        cot = ultima(moneda)
        if cot is None:
            chequeos.append(_c('error', f'Cotización {moneda}', "No hay ninguna cotización cargada."))
        elif cot.manual_activa:
            chequeos.append(_c('aviso', f'Cotización {moneda}', "Precio manual activo: no se actualiza sola."))
        elif cotizacion_vigente(cot):
            minutos = int((timezone.now() - cot.fecha).total_seconds() // 60)
            chequeos.append(_c('ok', f'Cotización {moneda}', f"Actualizada hace {minutos} min ({cot.fuente or 'sin fuente'})."))
        else:
            chequeos.append(_c('error', f'Cotización {moneda}', "Vencida: compra y venta están pausadas."))

    difs = diferencias_de_conciliacion(Usuario.objects.all())
    chequeos.append(_c('ok' if not difs else 'error', 'Conciliación de saldos',
                       "Los saldos de todos los clientes coinciden con el libro mayor." if not difs else
                       f"{len(difs)} saldo{'s' if len(difs) != 1 else ''} no coincide{'n' if len(difs) != 1 else ''} "
                       "con el libro mayor. Revisar antes de seguir operando."))

    hace_un_dia = timezone.now() - timedelta(hours=24)
    demorados = (DepositoARS.objects.filter(estado='pendiente', fecha__lt=hace_un_dia).count()
                 + DepositoUSDT.objects.filter(estado='pendiente', fecha__lt=hace_un_dia).count()
                 + RetiroARS.objects.filter(estado__in=('pendiente', 'aprobado'), fecha_solicitud__lt=hace_un_dia).count()
                 + RetiroCrypto.objects.filter(estado='pendiente', fecha_solicitud__lt=hace_un_dia).count())
    chequeos.append(_c('ok' if not demorados else 'aviso', 'Pendientes demorados',
                       "No hay depósitos ni retiros esperando más de 24 h." if not demorados else
                       f"{demorados} depósito{'s' if demorados != 1 else ''} o retiro{'s' if demorados != 1 else ''} "
                       "esperando más de 24 h."))

    usdt_error = DepositoUSDT.objects.filter(estado='pendiente', verificacion_estado='error').count()
    if usdt_error:
        chequeos.append(_c('aviso', 'Verificación on-chain',
                           f"Depósitos USDT que no se pudieron verificar en la red: {usdt_error}."))
    return chequeos


def configuracion():
    from usuarios.services.avisos import EVENTOS_EQUIPO, estado_servidor

    cfg = ExchangeConfig.current()
    chequeos = []
    if settings.DEMO_MODE:
        chequeos.append(_c('aviso', 'Modo demo', "Activo: se muestra el cartel de demostración y se usan datos de ejemplo."))
    chequeos.append(_c('error' if settings.DEBUG else 'ok', 'Modo debug',
                       "Activado: en producción expone información interna." if settings.DEBUG else "Desactivado."))
    hsts = getattr(settings, 'SECURE_HSTS_SECONDS', 0) or 0
    chequeos.append(_c('ok' if hsts >= 31536000 else 'aviso', 'HSTS',
                       f"{hsts} segundos." + ("" if hsts >= 31536000 else " Subir a 31536000 (1 año) cuando el dominio esté estable.")))
    servidor = estado_servidor()
    chequeos.append(_c('aviso' if servidor.startswith('Modo prueba') else 'ok', 'Envío de emails', servidor))
    nombres = {'depositos': 'depósitos', 'retiros': 'retiros', 'identidades': 'identidades', 'compliance': 'compliance'}
    vacios = [nombres[n] for n, campo in EVENTOS_EQUIPO.items() if n != 'soporte' and not getattr(cfg, campo)]
    chequeos.append(_c('ok' if not vacios else 'aviso', 'Destinatarios de avisos',
                       "Todos los avisos al equipo tienen destinatarios." if not vacios else
                       f"Sin destinatarios: {', '.join(vacios)} (Parámetros → Avisos por email)."))
    cuentas = [m for m, c in settings.CUENTAS_DEPOSITO.items() if not (c.get('cbu') or c.get('alias'))]
    chequeos.append(_c('ok' if not cuentas else 'aviso', 'Cuentas para depósitos',
                       "Cuentas en pesos y dólares cargadas." if not cuentas else
                       f"Falta la cuenta en {', '.join('pesos' if m == 'ARS' else 'dólares' for m in cuentas)} (CUENTA_*_ en el .env)."))
    chequeos.append(_c('ok' if any(settings.WALLETS_EMPRESA.values()) else 'aviso', 'Billeteras USDT',
                       "Cargadas: " + ', '.join(r for r, w in settings.WALLETS_EMPRESA.items() if w) + "."
                       if any(settings.WALLETS_EMPRESA.values()) else "No hay billeteras: los depósitos USDT están deshabilitados."))
    datos_ejemplo = settings.EMPRESA_CUIT == "30-00000000-0" or "Siempre Viva" in settings.EMPRESA_DOMICILIO
    chequeos.append(_c('aviso' if datos_ejemplo else 'ok', 'Datos legales de los comprobantes',
                       "Son datos de ejemplo (EMPRESA_CUIT, EMPRESA_DOMICILIO en el .env)." if datos_ejemplo
                       else "Cargados."))
    return chequeos


def errores_recientes(horas=24):
    return ErrorRegistrado.objects.filter(fecha__gte=timezone.now() - timedelta(hours=horas))


def limpiar_errores_viejos(dias=30):
    ErrorRegistrado.objects.filter(fecha__lt=timezone.now() - timedelta(days=dias)).delete()
