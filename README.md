# Finanxis Exchange

Exchange ARS / USD / USDT en Django 5.2: KYC, depósitos y retiros con aprobación manual,
compra/venta con spread, swap USD⇄USDT, boletos PDF verificables y contabilidad del exchange.

## Levantar con Docker (recomendado)

```bash
cp .env.example .env        # completar: SECRET_KEY, DB_PASSWORD, ALLOWED_HOSTS, datos legales...
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Servicios: `web` (gunicorn), `worker` y `beat` (Celery: actualiza la cotización cada
`COTIZACION_CADA_SEG`), `db` (PostgreSQL 16) y `redis`.

Generar una SECRET_KEY:

```bash
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```

## Desarrollo local sin Docker

```bash
python3 -m venv env && . env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # DEBUG=True y sin DB_NAME → usa SQLite
python manage.py migrate
python manage.py actualizar_cotizacion
python manage.py runserver
```

Tests: `python manage.py test usuarios` · Lint: `pip install -r requirements-dev.txt && ruff check .`

## Arquitectura

```
usuarios/
  views/          vistas por área (auth, cliente, operaciones, fondos, admin_panel, ...)
  services/
    trading.py    compra, venta y swap
    fondos.py     depósitos y retiros (incluye las transiciones del panel admin)
    ledger.py     libro mayor de doble entrada
    boletos.py    PDF de comprobantes
```

Las vistas solo validan la entrada; todo lo que mueve saldo pasa por `services/`, en una
transacción con bloqueo de fila. Las acciones del admin de Django usan los mismos servicios.

### Libro mayor

Cada movimiento de saldo genera un `Asiento` inmutable cuyas líneas suman 0 por moneda.
Cuentas: una por usuario, más `tesoreria`, `mesa`, `retiros_pendientes`, `comisiones` y `ajustes`.
`Usuario.saldo_*` es un caché que se actualiza en la misma transacción.

```bash
python manage.py ledger_apertura    # una vez, al migrar datos previos al mayor (idempotente)
python manage.py conciliar_saldos   # compara saldos vs. mayor; sale con código 1 si difieren
```

Celery beat corre la conciliación a diario y deja un `ERROR` en los logs si encuentra diferencias.
Un error en el mayor no se edita: se corrige con un asiento de ajuste.

## CI

GitHub Actions (`.github/workflows/ci.yml`) corre en cada push y PR: lint (ruff), migraciones
al día, `check --deploy` y los tests contra PostgreSQL (incluido un test de concurrencia).

## Producción: checklist

- `DEBUG=False`, `SECRET_KEY` propia, `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS` con el dominio real.
- HTTPS en el proxy (nginx / balanceador) enviando `X-Forwarded-Proto`; `AXES_IPWARE_PROXY_COUNT=1`.
- **No servir `/media/` desde el proxy**: DNI, comprobantes y boletos los entrega Django
  solo a su dueño o al staff (`usuarios/views/media.py`).
- Datos legales reales en `.env` (`EMPRESA_CUIT`, `EMPRESA_DOMICILIO`, `SUPPORT_CONTACTO`): salen impresos en los boletos.
- Email real (SMTP) para verificación de cuentas y recuperación de contraseña.
- Activar 2FA en todas las cuentas staff (Configuración → Seguridad).
- Backups diarios: `scripts/backup.sh` (ver cron en el script). Probar una restauración periódicamente.
- Cuando todo esté estable, subir `SECURE_HSTS_SECONDS` a `31536000`.

## Migrar datos desde SQLite

```bash
# con el .env de desarrollo (SQLite)
python manage.py dumpdata --natural-foreign --natural-primary \
  -e contenttypes -e auth.permission -e admin.logentry -e sessions -e axes -e django_celery_beat \
  -o datos.json

# en el stack Docker (base recién creada)
docker cp datos.json $(docker compose ps -q web):/tmp/datos.json
docker compose exec web sh -c "python manage.py loaddata /tmp/datos.json && rm /tmp/datos.json"
docker cp media/. $(docker compose ps -q web):/app/media/
rm datos.json   # contiene datos personales
docker compose exec web python manage.py ledger_apertura   # si los datos no traían el mayor
docker compose exec web python manage.py conciliar_saldos
```

## Seguridad implementada

- Bloqueo de login tras 5 intentos fallidos (django-axes), también para `/admin/`.
- 2FA TOTP (Google Authenticator, Authy) opcional por usuario.
- Archivos subidos con nombre aleatorio y acceso solo del dueño / staff.
- Operaciones con saldo en transacciones con bloqueo de fila; cotización vencida bloquea compra/venta.
