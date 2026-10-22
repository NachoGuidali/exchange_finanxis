#!/bin/sh
# Backup de PostgreSQL + archivos subidos (DNI, comprobantes, boletos) del stack Docker.
# Uso:      ./scripts/backup.sh [directorio_destino]
# Cron:     0 3 * * * cd /ruta/al/proyecto && ./scripts/backup.sh /backups >> /var/log/finanxis-backup.log 2>&1
#
# Los backups contienen datos personales: guardalos cifrados y fuera del servidor.
set -eu

DESTINO="${1:-./backups}"
RETENCION_DIAS="${RETENCION_DIAS:-14}"
FECHA="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DESTINO"
umask 077

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
    > "$DESTINO/db-$FECHA.dump"

docker compose exec -T web tar -czf - -C /app media > "$DESTINO/media-$FECHA.tar.gz"

find "$DESTINO" -name 'db-*.dump' -mtime +"$RETENCION_DIAS" -delete
find "$DESTINO" -name 'media-*.tar.gz' -mtime +"$RETENCION_DIAS" -delete

echo "[$FECHA] backup OK en $DESTINO"
