from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.timezone import now
import uuid
from django.conf import settings
from django.utils.text import slugify
from decimal import Decimal
from django.utils import timezone
from django.core.exceptions import ValidationError

from django.core.cache import cache
from django.core.validators import MinValueValidator, MaxValueValidator


BPS_MIN, BPS_MAX = 0, 5000  # 0% a 50%


# Rutas de subida con nombre aleatorio: los archivos privados (DNI, comprobantes)
# no deben tener nombres adivinables. Se sirven solo vía views_media.
def _nombre_aleatorio(carpeta, filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f"{carpeta}/{uuid.uuid4().hex}.{ext}"

def subir_dni(instance, filename):
    return _nombre_aleatorio('documentos', filename)

def subir_comprobante(instance, filename):
    return _nombre_aleatorio('comprobantes', filename)

def subir_comprobante_usdt(instance, filename):
    return _nombre_aleatorio('comprobantes_usdt', filename)

def subir_adjunto_ticket(instance, filename):
    return _nombre_aleatorio('tickets', filename)

class ExchangeConfig(models.Model):
    # guardo siempre una sola fila (singleton)
    singleton = models.BooleanField(default=True, editable=False, unique=True)

    # valores en basis points (bps)
    swap_fee_bps      = models.PositiveIntegerField(
        default=getattr(settings, "SWAP_FEE_BPS", 100),
        validators=[MinValueValidator(BPS_MIN), MaxValueValidator(BPS_MAX)],
        help_text="Comisión del swap en basis points (100 bps = 1%)."
    )
    spread_bps_usdt   = models.PositiveIntegerField(
        default=getattr(settings, "SPREAD_BPS_USDT", 200),
        validators=[MinValueValidator(BPS_MIN), MaxValueValidator(BPS_MAX)],
        help_text="Spread aplicado a USDT en bps."
    )
    spread_bps_usd    = models.PositiveIntegerField(
        default=getattr(settings, "SPREAD_BPS_USD", 200),
        validators=[MinValueValidator(BPS_MIN), MaxValueValidator(BPS_MAX)],
        help_text="Spread aplicado a USD en bps."
    )

    # Compliance (montos en ARS equivalentes). Los valores deben definirlos el Oficial de
    # Cumplimiento según la normativa vigente de UIF/CNV: estos defaults son solo de arranque.
    umbral_alerta_ars = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('5000000'),
        help_text="Una operación, depósito o retiro igual o mayor genera una alerta de compliance."
    )
    limite_mensual_operado_ars = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('50000000'),
        help_text="Volumen máximo de compra+venta por usuario en 30 días (se puede ajustar por usuario)."
    )
    limite_diario_retiro_ars = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('20000000'),
        help_text="Monto máximo de retiros (ARS + cripto valuado) por usuario en 24 h."
    )

    # Avisos por email al equipo: direcciones separadas por coma o en renglones distintos
    avisos_depositos   = models.TextField(blank=True, help_text="Depósitos informados por los clientes.")
    avisos_retiros     = models.TextField(blank=True, help_text="Retiros pedidos por los clientes.")
    avisos_identidades = models.TextField(blank=True, help_text="Clientes listos para verificar identidad.")
    avisos_compliance  = models.TextField(blank=True, help_text="Alertas de compliance.")
    avisos_soporte     = models.TextField(blank=True, help_text="Consultas de soporte (si está vacío, SOPORTE_TO).")

    updated_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "Configuración de Exchange"
        verbose_name_plural = "Configuración de Exchange"

    def save(self, *args, **kwargs):
        self.updated_at = timezone.now()
        super().save(*args, **kwargs)
        cache.delete("exchange_config_current")

    @classmethod
    def current(cls):
        """Obtiene (o crea) la única fila y la cachea."""
        obj = cache.get("exchange_config_current")
        if obj is None:
            obj, _ = cls.objects.get_or_create(singleton=True, defaults={})
            cache.set("exchange_config_current", obj, 60)  # 1 min (ajustable)
        return obj

    # helpers convenientes (devuelven decimales)
    @property
    def swap_fee_pct(self):
        # 100 bps = 0.01
        from decimal import Decimal
        return Decimal(self.swap_fee_bps) / Decimal("10000")

    @property
    def spread_usdt_pct(self):
        from decimal import Decimal
        return Decimal(self.spread_bps_usdt) / Decimal("10000")

    @property
    def spread_usd_pct(self):
        from decimal import Decimal
        return Decimal(self.spread_bps_usd) / Decimal("10000")

    def __str__(self):
        return "Configuración de Exchange"

# Create your models here.
class Pais(models.Model):
    iso2   = models.CharField(max_length=2, unique=True)
    nombre = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = "Paises"
        ordering = ["nombre"]

    def __str__(self):
        return f"{self.nombre} ({self.iso2})"


class Provincia(models.Model):
    pais      = models.ForeignKey(Pais, on_delete=models.CASCADE, related_name="provincias")
    nombre    = models.CharField(max_length=120)
    slug      = models.SlugField(max_length=140, blank=True)
    georef_id = models.CharField(max_length=10, blank=True)  # id oficial Georef-AR opcional

    class Meta:
        unique_together = [("pais", "nombre")]
        ordering = ["nombre"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.nombre)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} — {self.pais.nombre}"


class Localidad(models.Model):
    provincia = models.ForeignKey(Provincia, on_delete=models.CASCADE, related_name="localidades")
    nombre    = models.CharField(max_length=140)
    slug      = models.SlugField(max_length=160, blank=True)

    class Meta:
        unique_together = [("provincia", "nombre")]
        ordering = ["nombre"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.nombre)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} — {self.provincia.nombre}"


PERSONA_TIPO = (
    ("FISICA",   "Persona física"),
    ("JURIDICA", "Persona jurídica"),
)

ESTADO_CIVIL = (
    ("SOLTERO", "Soltero/a"),
    ("CASADO",  "Casado/a"),
    ("DIVORCIADO", "Divorciado/a"),
    ("VIUDO", "Viudo/a"),
    ("CONVIVIENTE", "Unión convivencial"),
    ("OTRO", "Otro"),
)

SEXO_CHOICES = (
    ("M", "Masculino"),
    ("F", "Femenino"),
    ("X", "No binario / X"),
    ("ND", "Prefiero no decir"),
)


class Usuario(AbstractUser):
    # KYC (ya estaban)
    dni_frente = models.ImageField(upload_to=subir_dni, null=True, blank=True)
    dni_dorso  = models.ImageField(upload_to=subir_dni, null=True, blank=True)
    estado_verificacion = models.CharField(
        max_length=20,
        choices=[('pendiente','Pendiente'),('aprobado','Aprobado'),('rechazado','Rechazado')],
        default='pendiente'
    )

    # Saldos (ya estaban)
    saldo_ars  = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    saldo_usdt = models.DecimalField(max_digits=26, decimal_places=6, default=0)  # USDT: 6 decimales
    saldo_usd  = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    # Documento / identidad
    doc_tipo = models.CharField(max_length=8, default='DNI')
    doc_nro  = models.CharField(max_length=32, blank=True)

    persona_tipo     = models.CharField(max_length=10, choices=PERSONA_TIPO, default="FISICA")
    estado_civil     = models.CharField(max_length=20, choices=ESTADO_CIVIL, blank=True)
    sexo             = models.CharField(max_length=10, choices=SEXO_CHOICES, blank=True)
    nacionalidad     = models.CharField(max_length=32, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    lugar_nacimiento = models.CharField(max_length=128, blank=True)

    # Domicilio estructurado
    pais          = models.ForeignKey(Pais, null=True, blank=True, on_delete=models.SET_NULL, related_name="usuarios")
    provincia     = models.ForeignKey(Provincia, null=True, blank=True, on_delete=models.SET_NULL, related_name="usuarios")
    localidad     = models.ForeignKey(Localidad, null=True, blank=True, on_delete=models.SET_NULL, related_name="usuarios")
    codigo_postal = models.CharField(max_length=16, blank=True)
    calle         = models.CharField(max_length=128, blank=True)
    numero_calle  = models.CharField(max_length=16, blank=True)
    piso          = models.CharField(max_length=16, blank=True)
    depto         = models.CharField(max_length=16, blank=True)

    # Compat antiguos
    domicilio = models.CharField(max_length=255, blank=True)
    telefono  = models.CharField(max_length=32, blank=True)

    # Términos y Condiciones
    tyc_aceptado    = models.BooleanField(default=False)
    tyc_version     = models.CharField(max_length=20, blank=True)
    tyc_aceptado_en = models.DateTimeField(null=True, blank=True)

    #confirmacion de email
    email_confirmed = models.BooleanField(default=False)
    email_confirmed_at = models.DateTimeField(null=True, blank=True)
    email_confirm_sent_at = models.DateTimeField(null=True, blank=True)

    # 2FA (TOTP, compatible con Google Authenticator / Authy)
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_activo = models.BooleanField(default=False)

    # Límite propio de volumen mensual (ARS); vacío = usa el general de ExchangeConfig
    limite_mensual_ars = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    @property
    def declaracion_vigente(self):
        """La última declaración jurada UIF del usuario, o None si nunca la completó."""
        return self.declaraciones.first()


    def __str__(self):
        return self.username

    @property
    def direccion_full(self):
        partes = [
            f"{self.calle} {self.numero_calle}".strip(),
            f"Piso {self.piso} Dpto {self.depto}".strip() if (self.piso or self.depto) else "",
            self.localidad.nombre if self.localidad else "",
            self.provincia.nombre if self.provincia else "",
            self.pais.nombre if self.pais else "",
            self.codigo_postal or "",
        ]
        return ", ".join([p for p in partes if p])

    # Enforce inmutables (nombre, apellido, doc)
    def save(self, *args, **kwargs):
        if self.pk:
            orig = type(self).objects.get(pk=self.pk)
            changed_identity = (
                orig.first_name != self.first_name or
                orig.last_name  != self.last_name  or
                orig.doc_tipo   != self.doc_tipo   or
                orig.doc_nro    != self.doc_nro
            )
            # Permití override manual si alguna vez lo necesitás (ej. admin):
            if changed_identity and not getattr(self, "_allow_identity_update", False):
                raise ValidationError("Nombre/Apellido y Documento no se pueden modificar una vez guardados.")
        return super().save(*args, **kwargs)

    def marcar_tyc_aceptado(self, version: str):
        self.tyc_aceptado = True
        self.tyc_version = version
        self.tyc_aceptado_en = timezone.now()
        self.save(update_fields=["tyc_aceptado","tyc_version","tyc_aceptado_en"])
    

class DepositoARS(models.Model):
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('rechazado', 'Rechazado'),
    ]    

    MONEDAS = [('ARS', 'Pesos'), ('USD', 'Dólares')]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # depósito por transferencia bancaria: en pesos o en dólares
    moneda = models.CharField(max_length=4, choices=MONEDAS, default='ARS')
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    comprobante = models.ImageField(upload_to=subir_comprobante)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    fecha = models.DateTimeField(auto_now_add=True)

    @property
    def simbolo(self):
        return 'US$' if self.moneda == 'USD' else '$'

    def __str__(self):
        return f"{self.usuario.username} - {self.simbolo}{self.monto} - {self.estado}"
    
    
class DepositoUSDT(models.Model):
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('rechazado', 'Rechazado'),
    ]

    REDES = [
        ('TRC20', 'TRC20'),
        ('ERC20', 'ERC20'),
        ('BEP20', 'BEP20'),
        ('SOL', 'Solana'),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    monto = models.DecimalField(max_digits=26, decimal_places=6)
    red = models.CharField(max_length=10, choices=REDES)
    txid = models.CharField(max_length=200)
    comprobante = models.ImageField(upload_to=subir_comprobante_usdt)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    fecha = models.DateTimeField(auto_now_add=True)

    # Verificación on-chain (services/onchain.py)
    VERIFICACION = [
        ('sin_verificar', 'Sin verificar'),
        ('pendiente', 'Esperando confirmaciones'),
        ('verificado', 'Verificado'),
        ('no_coincide', 'No coincide'),
        ('no_soportada', 'Red no soportada'),
        ('error', 'Error al consultar'),
    ]
    verificacion_estado  = models.CharField(max_length=20, choices=VERIFICACION, default='sin_verificar')
    verificacion_detalle = models.CharField(max_length=255, blank=True)
    monto_onchain        = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)
    verificado_en        = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.monto} USDT - {self.estado}"


class Movimiento(models.Model):
    TIPO_CHOICES = [
        ('deposito', 'Depósito'),
        ('retiro', 'Retiro'),
        ('compra', 'Compra'),
        ('venta', 'Venta'),
        ('ajuste', 'Ajuste manual'),
    ]

    MONEDA_CHOICES = [
        ('ARS', 'Pesos ARS'),
        ('USDT', 'USDT'),
        ('USD', 'USD'),
    ]

    codigo = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    moneda = models.CharField(max_length=10, choices=MONEDA_CHOICES)
    # 6 decimales para USDT; pesos y dólares se redondean a 2 en services
    monto = models.DecimalField(max_digits=26, decimal_places=6)
    saldo_antes = models.DecimalField(max_digits=26, decimal_places=6, null=True, blank=True)
    saldo_despues = models.DecimalField(max_digits=26, decimal_places=6, null=True, blank=True)
    admin_responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acciones_admin'
    )
    fecha = models.DateTimeField(auto_now_add=True)
    descripcion = models.TextField(blank=True, null=True)
    # Agrupa las dos patas de una compra, venta o swap (ARS que sale y USDT que entra, etc.)
    operacion = models.UUIDField(null=True, blank=True, db_index=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.tipo} - {self.moneda} {self.monto}"


class Cotizacion(models.Model):
    MONEDAS = [
        ('USDT', 'USDT'),
        ('USD', 'USD'),
    ]

    moneda = models.CharField(max_length=10, choices=MONEDAS)
    compra = models.DecimalField(max_digits=20, decimal_places=2)  
    venta = models.DecimalField(max_digits=20, decimal_places=2)   
    fecha = models.DateTimeField(auto_now_add=True)

    ref_compra = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    ref_venta  = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)

    margin_bps = models.IntegerField(null=True, blank=True)

    fuente = models.CharField(max_length=40, blank=True, default='')
    # Precio cargado a mano por un admin: la actualización automática no lo pisa mientras esté activo.
    # manual_hasta vacío = hasta que lo quiten.
    manual = models.BooleanField(default=False)
    manual_hasta = models.DateTimeField(null=True, blank=True)
    cargada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='+')

    @property
    def manual_activa(self):
        return self.manual and (self.manual_hasta is None or timezone.now() < self.manual_hasta)

    def __str__(self):
        return f"{self.moneda} - Compra: {self.compra} / Venta: {self.venta}"

class RetiroARS(models.Model):
    ESTADOS = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('enviado', 'Enviado'),
        ('rechazado', 'Reachazado'),
    ]

    MONEDAS = [('ARS', 'Pesos'), ('USD', 'Dólares')]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # retiro por transferencia bancaria: a una cuenta en pesos o en dólares
    moneda = models.CharField(max_length=4, choices=MONEDAS, default='ARS')
    alias = models.CharField(max_length=50, blank=True)
    cbu = models.CharField(max_length=30, blank=True, null=True)
    banco = models.CharField(max_length=50, blank=True, null=True)
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    estado = models.CharField(max_length=20, choices=ESTADOS, default='pendiente')
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    # quien envía confirma que la cuenta de destino es del cliente; si no, rechaza con motivo
    titular_verificado = models.BooleanField(default=False)
    motivo_rechazo = models.CharField(max_length=255, blank=True)

    @property
    def simbolo(self):
        return 'US$' if self.moneda == 'USD' else '$'

    @property
    def destino(self):
        return ' / '.join(x for x in (self.alias, self.cbu) if x)


class Notificacion(models.Model):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    mensaje = models.TextField()
    leida = models.BooleanField(default=False)
    fecha = models.DateTimeField(default=now)

    def __str__(self):
        return f"[{self.usuario.username}] {self.mensaje[:40]}"    

class RetiroCrypto(models.Model):
    MONEDAS = (
        ('USDT', 'USDT'),
        ('USD', 'USD'),
    )        

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    moneda = models.CharField(max_length=4, choices=MONEDAS)
    monto = models.DecimalField(max_digits=26, decimal_places=6)
    direccion_wallet = models.CharField(max_length=255)
    red = models.CharField(max_length=10, blank=True)  # elegida por el cliente (TRC20, ERC20, BEP20)
    estado = models.CharField(max_length=20, choices=[('pendiente', 'Pendiente'), ('enviado', 'Enviado'), ('rechazado', 'Rechazado')], default='pendiente')
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    admin_responsable =models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='retiros_cripto_aprobados')
    motivo_rechazo = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.moneda} - {self.monto}"
    


class BoletoOperacion(models.Model):
    TIPO = [
        ('compra_ars_usdt', 'Compra USDT con ARS'),
        ('compra_ars_usd',  'Compra USD con ARS'),
        ('venta_usdt_ars',  'Venta USDT por ARS'),
        ('venta_usd_ars',   'Venta USD por ARS'),
        ('swap_usd_usdt',   'Swap USD→USDT'),
        ('swap_usdt_usd',   'Swap USDT→USD'),
        ('deposito_ars',    'Depósito ARS'),
        ('deposito_usdt',   'Depósito USDT'),
        ('deposito_usd',    'Depósito USD'),
        ('retiro_ars',      'Retiro ARS'),
        ('retiro_usdt',     'Retiro USDT'),
        ('retiro_usd',      'Retiro USD'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    movimiento = models.ForeignKey('usuarios.Movimiento', on_delete=models.SET_NULL, null=True, blank=True)
    tipo = models.CharField(max_length=32, choices=TIPO)
    numero = models.CharField(max_length=40, unique=True)          # ej: BOL-20250826-000123
    fecha_emision = models.DateTimeField(default=now)               # guardar con segundos
    snapshot = models.JSONField()                                   # todos los datos impresos
    pdf = models.FileField(upload_to='boletos/')
    pdf_sha256 = models.CharField(max_length=64)
    verificacion_code = models.CharField(max_length=64, unique=True)  # uuid/nonce
    anulado = models.BooleanField(default=False)  # por si necesitás anular con contracomprobante

    # Datos blockchain opcionales (para no abrir JSON):
    red = models.CharField(max_length=32, blank=True)
    wallet_origen = models.CharField(max_length=255, blank=True)
    wallet_destino = models.CharField(max_length=255, blank=True)
    txid = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-fecha_emision']    


class SupportTicket(models.Model):
    CATEGORIAS = [
        ("cuenta", "Cuenta / Perfil"),
        ("operaciones", "Operaciones (comprar/vender/swap)"),
        ("verificacion", "Verificación KYC"),
        ("pagos", "Depósitos / Retiros"),
        ("otro", "Otro"),
    ]
    PRIORIDADES = [("baja","Baja"),("media","Media"),("alta","Alta")]

    usuario   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets")
    email     = models.EmailField()
    asunto    = models.CharField(max_length=150)
    categoria = models.CharField(max_length=32, choices=CATEGORIAS, default="otro")
    prioridad = models.CharField(max_length=16, choices=PRIORIDADES, default="media")
    mensaje   = models.TextField()
    ESTADOS = [
        ("abierto", "Abierto"),          # espera respuesta del equipo
        ("respondido", "Respondido"),    # espera al cliente
        ("cerrado", "Cerrado"),
    ]

    adjunto   = models.FileField(upload_to=subir_adjunto_ticket, blank=True, null=True)
    estado    = models.CharField(max_length=20, choices=ESTADOS, default="abierto", db_index=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-creado_en",)

    def __str__(self):
        return f"#{self.id} {self.asunto}"


class RespuestaTicket(models.Model):
    """Conversación de un ticket: mensajes del cliente y del equipo de soporte."""
    ticket    = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="respuestas")
    autor     = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    es_staff  = models.BooleanField(default=False)
    mensaje   = models.TextField()
    creado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("creado_en", "id")
    


## CONTABILIDAD

class CuentaExchange(models.Model):
    nombre = models.CharField(max_length=50, default="Exchange", unique=True)
    saldo_ars  = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    saldo_usdt = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    saldo_usd  = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    def __str__(self):
        return self.nombre


class ApunteExchange(models.Model):
    MONEDAS = (("ARS","ARS"),("USDT","USDT"),("USD","USD"))

    # --- Nuevo: para matchear con tus create() ---
    CATEGORIAS = [
        ('spread_compra', 'Spread en COMPRA'),
        ('spread_venta',  'Spread en VENTA'),
        ('fee_swap',      'Comisión de SWAP'),
        ('entrada',       'Entrada (depósito acreditado)'),
        ('salida',        'Salida (retiro enviado)'),
        ('ajuste',        'Ajuste contable'),
    ]

    fecha = models.DateTimeField(default=timezone.now)

    # Usuario relacionado (beneficiario u origen del movimiento)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL)

    # Mantengo tu campo "tipo" para compatibilidad (opcional)
    TIPO = (
        ("comision_swap","Comisión SWAP"),
        ("spread_compra","Spread en COMPRA"),
        ("spread_venta","Spread en VENTA"),
        ("ajuste","Ajuste contable"),
        ("ingreso","Ingreso"),   # por si lo usás en algún reporte
        ("egreso","Egreso"),
    )
    tipo = models.CharField(max_length=30, choices=TIPO, default="ingreso")

    # --- Campos de pricing y categorización (nuevos) ---
    categoria     = models.CharField(max_length=40, choices=CATEGORIAS, default='ajuste')

    # Moneda y montos (tus nombres + los que usan tus create())
    moneda        = models.CharField(max_length=10, choices=MONEDAS)
    monto_moneda  = models.DecimalField(max_digits=20, decimal_places=6)   # alias de 'importe'
    monto_ars     = models.DecimalField(max_digits=20, decimal_places=2)   # alias de 'importe_ars'

    # Aliases con nombres que tu código ya está usando
    # (no son fields; se persiste en monto_moneda / monto_ars)
    @property
    def importe(self):
        return self.monto_moneda
    @importe.setter
    def importe(self, v):
        self.monto_moneda = v

    @property
    def importe_ars(self):
        return self.monto_ars
    @importe_ars.setter
    def importe_ars(self, v):
        self.monto_ars = v

    # Referencia al movimiento (nuevo)
    movimiento = models.ForeignKey('usuarios.Movimiento', null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='apuntes_exchange')

    # Pricing de referencia
    ref_price     = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    applied_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    # Bolsa para datos extra (nuevo)
    extra         = models.JSONField(default=dict, blank=True)

    # Tu campo previo para trazabilidad adicional (lo dejo por compatibilidad)
    ref_movimiento = models.CharField(max_length=64, blank=True)
    detalle        = models.TextField(blank=True)

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f"{self.fecha:%Y-%m-%d %H:%M} {self.categoria} {self.monto_moneda} {self.moneda}"

## LIBRO MAYOR (doble entrada)
#
# Cada operación que mueve saldo genera un Asiento con líneas cuya suma, por moneda, es 0.
# Convención de signo: monto > 0 acredita la cuenta, monto < 0 la debita.
#   - Cuentas de usuario (lo que el exchange le debe al cliente): saldo = suma de sus líneas.
#   - Tesorería (bancos y wallets del exchange): queda en negativo cuando entra dinero,
#     su valor absoluto es lo que el exchange tiene custodiado.
# Los saldos en Usuario.saldo_* son un caché; `manage.py conciliar_saldos` los verifica.

class CuentaContable(models.Model):
    TESORERIA = 'tesoreria'                    # bancos y wallets del exchange
    MESA = 'mesa'                              # contraparte de compras, ventas y swaps
    RETIROS_PENDIENTES = 'retiros_pendientes'  # saldo reservado de retiros no enviados
    COMISIONES = 'comisiones'                  # fees cobrados
    AJUSTES = 'ajustes'                        # aperturas y ajustes manuales

    codigo  = models.CharField(max_length=64, unique=True)
    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='cuenta_contable')

    def __str__(self):
        return self.codigo


class Asiento(models.Model):
    TIPOS = [
        ('apertura', 'Saldo de apertura'),
        ('deposito', 'Depósito acreditado'),
        ('compra', 'Compra'),
        ('venta', 'Venta'),
        ('swap', 'Swap'),
        ('retiro_solicitado', 'Retiro solicitado'),
        ('retiro_enviado', 'Retiro enviado'),
        ('retiro_rechazado', 'Retiro rechazado'),
        ('ajuste', 'Ajuste manual'),
    ]

    fecha       = models.DateTimeField(default=timezone.now, db_index=True)
    tipo        = models.CharField(max_length=32, choices=TIPOS)
    descripcion = models.CharField(max_length=255)
    referencia  = models.CharField(max_length=64, blank=True, db_index=True)  # ej. "RetiroARS:15"
    creado_por  = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-fecha', '-id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Los asientos son inmutables: registrá un asiento de ajuste.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Los asientos son inmutables: registrá un asiento de ajuste.")

    def __str__(self):
        return f"#{self.pk} {self.tipo} {self.descripcion}"


class LineaAsiento(models.Model):
    MONEDAS = (("ARS", "ARS"), ("USDT", "USDT"), ("USD", "USD"))

    asiento = models.ForeignKey(Asiento, on_delete=models.PROTECT, related_name='lineas')
    cuenta  = models.ForeignKey(CuentaContable, on_delete=models.PROTECT, related_name='lineas')
    moneda  = models.CharField(max_length=10, choices=MONEDAS)
    monto   = models.DecimalField(max_digits=20, decimal_places=6)

    class Meta:
        indexes = [models.Index(fields=['cuenta', 'moneda'])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Las líneas de asiento son inmutables.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Las líneas de asiento son inmutables.")

    def __str__(self):
        return f"{self.cuenta} {self.moneda} {self.monto}"


## COMPLIANCE Y AUDITORÍA

class RegistroAuditoria(models.Model):
    """Quién hizo qué y desde dónde. Inmutable."""
    fecha   = models.DateTimeField(default=timezone.now, db_index=True)
    actor   = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.PROTECT, related_name='+')
    accion  = models.CharField(max_length=64, db_index=True)
    objeto  = models.CharField(max_length=128, blank=True, db_index=True)  # ej. "DepositoARS:12"
    detalle = models.JSONField(default=dict, blank=True)
    ip      = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-fecha', '-id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("El registro de auditoría es inmutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("El registro de auditoría es inmutable.")

    def __str__(self):
        return f"{self.fecha:%Y-%m-%d %H:%M} {self.actor} {self.accion} {self.objeto}"


class AlertaCompliance(models.Model):
    TIPOS = [
        ('umbral', 'Operación igual o mayor al umbral'),
        ('fraccionamiento', 'Posible fraccionamiento (varias operaciones que suman el umbral)'),
        ('retiro_rapido', 'Retiro dentro de las 24 h de un depósito'),
        ('limite_excedido', 'Intento de superar un límite'),
        ('pep', 'Cliente declarado Persona Expuesta Políticamente'),
    ]
    ESTADOS = [
        ('abierta', 'Abierta'),
        ('descartada', 'Descartada'),
        ('reportada', 'Reportada (ROS)'),
    ]

    fecha       = models.DateTimeField(default=timezone.now, db_index=True)
    usuario     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name='alertas_compliance')
    tipo        = models.CharField(max_length=32, choices=TIPOS)
    detalle     = models.CharField(max_length=255)
    monto_ars   = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    referencia  = models.CharField(max_length=64, blank=True)
    estado      = models.CharField(max_length=16, choices=ESTADOS, default='abierta', db_index=True)
    revisada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name='+')
    revisada_en = models.DateTimeField(null=True, blank=True)
    nota        = models.TextField(blank=True)

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f"{self.get_tipo_display()} — {self.usuario} — {self.estado}"


class DeclaracionJurada(models.Model):
    """
    Declaraciones juradas que la normativa UIF le exige a un PSAV respecto de cada cliente:
    condición de Persona Expuesta Políticamente, Sujeto Obligado, origen de los fondos,
    actividad, perfil de ingresos y residencia fiscal. Cada cambio es una versión nueva:
    la vigente es la última y las anteriores quedan como historial.
    """
    PEP_TIPOS = [
        ('titular', 'Soy Persona Expuesta Políticamente'),
        ('familiar', 'Soy familiar de una PEP'),
        ('allegado', 'Soy allegado cercano de una PEP'),
    ]
    ORIGENES = [
        ('sueldo', 'Sueldo en relación de dependencia'),
        ('independiente', 'Actividad independiente o profesional'),
        ('comercial', 'Actividad comercial o empresaria'),
        ('jubilacion', 'Jubilación o pensión'),
        ('ahorros', 'Ahorros propios'),
        ('venta_bienes', 'Venta de bienes'),
        ('herencia', 'Herencia o donación'),
        ('inversiones', 'Rentas o inversiones'),
        ('otro', 'Otro'),
    ]
    RANGOS = [
        ('hasta_1m', 'Hasta $ 1.000.000'),
        ('1m_5m', 'De $ 1.000.000 a $ 5.000.000'),
        ('5m_20m', 'De $ 5.000.000 a $ 20.000.000'),
        ('mas_20m', 'Más de $ 20.000.000'),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='declaraciones')
    fecha = models.DateTimeField(default=timezone.now, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    version = models.CharField(max_length=20)

    es_pep = models.BooleanField()
    pep_tipo = models.CharField(max_length=10, choices=PEP_TIPOS, blank=True)
    pep_cargo = models.CharField(max_length=150, blank=True)
    pep_organismo = models.CharField(max_length=150, blank=True)

    es_sujeto_obligado = models.BooleanField()
    sujeto_obligado_actividad = models.CharField(max_length=150, blank=True)

    actividad = models.CharField(max_length=150)
    origen_fondos = models.CharField(max_length=20, choices=ORIGENES)
    origen_detalle = models.CharField(max_length=255, blank=True)
    ingresos_mensuales = models.CharField(max_length=10, choices=RANGOS)
    volumen_mensual_previsto = models.CharField(max_length=10, choices=RANGOS)

    residencia_fiscal_exterior = models.BooleanField()
    residencia_fiscal_pais = models.CharField(max_length=80, blank=True)
    residencia_fiscal_nif = models.CharField(max_length=40, blank=True)

    declara_veracidad = models.BooleanField()

    class Meta:
        ordering = ['-fecha', '-id']

    def __str__(self):
        return f"DDJJ {self.usuario.username} {self.fecha:%d/%m/%Y}"


class CotizacionReservada(models.Model):
    """Precio bloqueado unos segundos para que el cliente confirme sin sorpresas."""
    OPERACIONES = [('compra', 'Compra'), ('venta', 'Venta')]

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usuario     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    operacion   = models.CharField(max_length=8, choices=OPERACIONES)
    moneda      = models.CharField(max_length=10)
    monto       = models.DecimalField(max_digits=26, decimal_places=6)  # ARS en compra, moneda en venta
    precio      = models.DecimalField(max_digits=20, decimal_places=2)
    precio_ref  = models.DecimalField(max_digits=20, decimal_places=6)
    resultado   = models.DecimalField(max_digits=26, decimal_places=6)  # lo que recibe el cliente
    cotizacion  = models.ForeignKey(Cotizacion, on_delete=models.PROTECT)
    creada      = models.DateTimeField(auto_now_add=True)
    expira      = models.DateTimeField()
    usada_en    = models.DateTimeField(null=True, blank=True)

    @property
    def vigente(self):
        return self.usada_en is None and timezone.now() < self.expira
