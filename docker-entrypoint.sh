#!/bin/sh
set -e

# Solo el servicio web aplica migraciones (worker y beat arrancan con RUN_MIGRATIONS=0)
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    python manage.py migrate --noinput
    # cotización inicial para no esperar el primer ciclo de beat (si la API falla, se reintenta luego)
    python manage.py actualizar_cotizacion || true
fi

exec "$@"
