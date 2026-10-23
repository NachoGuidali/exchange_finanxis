#!/usr/bin/env bash
# Prepara la demo en PythonAnywhere (cuenta free). Correr desde una consola Bash de PythonAnywhere:
#
#   bash ~/exchange_finanxis/scripts/demo_pythonanywhere.sh 'ClaveParaLaDemo123'
#
# Es idempotente: se puede volver a correr para actualizar (pip, migraciones, estáticos).
# Si la web app ya existe, escribe su archivo WSGI y la recarga.
# Cuentas en la región EU: DOMINIO=USUARIO.eu.pythonanywhere.com bash scripts/demo_pythonanywhere.sh ...
set -euo pipefail

PASSWORD="${1:?Uso: bash scripts/demo_pythonanywhere.sh <contraseña-para-los-usuarios-demo>}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO="$(whoami)"
DOMINIO="${DOMINIO:-$USUARIO.pythonanywhere.com}"
PY="${PY:-python3.12}"
VENV="${VENV:-$HOME/.virtualenvs/finanxis}"
WSGI="${WSGI:-/var/www/${DOMINIO//./_}_wsgi.py}"

cd "$APP_DIR"

echo "==> Virtualenv en $VENV"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# sin caché: la cuenta free tiene 512 MB de disco
"$VENV/bin/python" -m pip install --quiet --no-cache-dir -r requirements.txt

if [ ! -f .env ]; then
    echo "==> Creando .env para $DOMINIO"
    clave="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(50))')"
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$clave|" -e "s|USUARIO.pythonanywhere.com|$DOMINIO|g" \
        .env.demo.example > .env
    chmod 600 .env
fi

echo "==> Base de datos y estáticos"
"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py collectstatic --noinput --verbosity 0
"$VENV/bin/python" manage.py cargar_demo --password "$PASSWORD"

if "$VENV/bin/python" -c "import weasyprint; weasyprint.HTML(string='<p>ok</p>').write_pdf()" 2>/dev/null; then
    echo "==> WeasyPrint OK: las operaciones generan boleto PDF"
else
    echo "==> WeasyPrint no funciona en este servidor: la demo anda igual, sin boletos PDF"
fi

if [ -f "$WSGI" ]; then
    cat > "$WSGI" <<EOF
import sys

path = '$APP_DIR'
if path not in sys.path:
    sys.path.insert(0, path)

from core_exchange.wsgi import application  # noqa
EOF
    echo "==> WSGI escrito en $WSGI; recargando https://$DOMINIO"
    touch "$WSGI"
else
    cat <<EOF

==> Falta crear la web app (pestaña Web de PythonAnywhere):
    1. Add a new web app → Manual configuration → Python ${PY#python}
    2. Virtualenv: $VENV
    3. Activar Force HTTPS
    4. Volver a correr este script: escribe el WSGI y recarga la app
EOF
fi
