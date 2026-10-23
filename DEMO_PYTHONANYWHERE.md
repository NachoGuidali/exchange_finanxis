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

## 1. Cuenta

Crear una cuenta *Beginner* (gratis) en https://www.pythonanywhere.com. La demo queda en
`https://USUARIO.pythonanywhere.com`.

## 2. Instalar (consola Bash de PythonAnywhere: *Consoles → Bash*)

```bash
git clone https://github.com/NachoGuidali/exchange_finanxis.git
bash exchange_finanxis/scripts/demo_pythonanywhere.sh 'UnaClaveParaLaDemo123'
```

El script crea el virtualenv, instala dependencias, genera el `.env` con una `SECRET_KEY` nueva,
migra, junta los estáticos y carga los datos demo: `demo_admin` (superusuario), `demo_cliente`
(KYC aprobado, con saldo e historial) y `demo_nuevo` (KYC pendiente, para mostrar la aprobación).
Todos con esa contraseña. Tarda unos minutos (la instalación de paquetes es lo más lento).

## 3. Web app (pestaña *Web*)

1. *Add a new web app* → *Next* → **Manual configuration** (no "Django") → **Python 3.12**.
2. Sección *Virtualenv*: `/home/USUARIO/.virtualenvs/finanxis`
3. Sección *Security*: activar **Force HTTPS**.
4. **No agregues `/media/`** en *Static files*: los archivos subidos los entrega la app solo a su
   dueño o al staff (los estáticos ya los sirve whitenoise).
5. En la consola Bash, volver a correr el script: escribe el archivo WSGI y recarga la app.

   ```bash
   bash exchange_finanxis/scripts/demo_pythonanywhere.sh 'UnaClaveParaLaDemo123'
   ```

Listo: entrar a `https://USUARIO.pythonanywhere.com`.

## 4. Mantenimiento

- La web app free vence al mes: entrá a la pestaña *Web* y tocá *Run until 1 month from today*.
- Para actualizar el código: `cd exchange_finanxis && git pull` y volver a correr el script (recarga solo).
- Errores: pestaña *Web* → *Error log*.
