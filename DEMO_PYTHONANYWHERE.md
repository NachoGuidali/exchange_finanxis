# Demo en PythonAnywhere (cuenta free)

Para **mostrar** la app, no para operar con dinero real. En la cuenta free no hay PostgreSQL,
Celery ni Redis, el internet saliente está limitado a una lista de sitios permitidos y hay
100 segundos de CPU por día. El modo demo lo compensa:

| Limitación free | Cómo se resuelve en modo demo |
|---|---|
| Sin Celery (cotización automática) | La cotización vencida se actualiza al entrar a *Operar* |
| `p2p.binance.com` no está permitido | USDT se cotiza con Binance spot (`api.binance.com`, permitido) |
| Sin PostgreSQL | SQLite |
| WeasyPrint puede no tener las librerías del sistema | Si falla el PDF, la operación sigue sin boleto |
| Sin email | Usuarios demo ya verificados; links de verificación en `verification_links.log` |

> **Nunca subas `db.sqlite3` ni `media/` reales**: tienen DNI y datos personales.
> La demo se carga con datos ficticios (`cargar_demo`).

## 1. Código y entorno (consola Bash de PythonAnywhere)

El repo es privado: creá un *personal access token* de GitHub con permiso de lectura.

```bash
git clone https://<TOKEN>@github.com/NachoGuidali/exchange_finanxis.git
cd exchange_finanxis
mkvirtualenv --python=python3.12 finanxis
pip install -r requirements.txt
```

## 2. Configuración

```bash
cp .env.demo.example .env
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
nano .env    # pegar la SECRET_KEY y reemplazar USUARIO por tu usuario de PythonAnywhere
```

## 3. Base de datos y datos demo

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py cargar_demo --password 'UnaClaveParaLaDemo123'
```

Crea `demo_admin` (superusuario), `demo_cliente` (KYC aprobado, con saldo e historial) y
`demo_nuevo` (KYC pendiente, para mostrar la aprobación). Todos con esa contraseña.

Opcional: ver si WeasyPrint funciona en el servidor (si no, la demo anda igual, sin boletos PDF):

```bash
python -c "import weasyprint; weasyprint.HTML(string='<p>ok</p>').write_pdf(); print('PDF OK')"
```

## 4. Web app (pestaña *Web*)

1. *Add a new web app* → *Manual configuration* → **Python 3.12**.
2. *Virtualenv*: `/home/USUARIO/.virtualenvs/finanxis`
3. *WSGI configuration file*: reemplazar todo el contenido por

   ```python
   import os, sys

   path = '/home/USUARIO/exchange_finanxis'
   if path not in sys.path:
       sys.path.insert(0, path)
   os.environ['DJANGO_SETTINGS_MODULE'] = 'core_exchange.settings'

   from django.core.wsgi import get_wsgi_application
   application = get_wsgi_application()
   ```

4. *Static files*: `/static/` → `/home/USUARIO/exchange_finanxis/staticfiles`
   (opcional, whitenoise ya los sirve). **No agregues `/media/`**: los archivos subidos
   solo los entrega la app a su dueño o al staff.
5. Activar **Force HTTPS**.
6. **Reload**.

## 5. Mantenimiento

- La web app free vence al mes: entrá a la pestaña *Web* y tocá *Run until 1 month from today*.
- Para actualizar el código: `git pull`, `python manage.py migrate`, `collectstatic` y *Reload*.
- Errores: pestaña *Web* → *Error log*.
