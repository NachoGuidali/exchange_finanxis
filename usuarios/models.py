from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.timezone import now
import uuid
from django.conf import settings
from django.utils.text import slugify
from django.utils import timezone
from django.core.exceptions import ValidationError


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
    dni_frente = models.ImageField(upload_to='documentos/', null=True, blank=True)
    dni_dorso  = models.ImageField(upload_to='documentos/', null=True, blank=True)
    estado_verificacion = models.CharField(
        max_length=20,
        choices=[('pendiente','Pendiente'),('aprobado','Aprobado'),('rechazado','Rechazado')],
        default='pendiente'
    )

    # Saldos (ya estaban)
    saldo_ars  = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    saldo_usdt = models.DecimalField(max_digits=20, decimal_places=2, default=0)
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

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    comprobante = models.ImageField(upload_to='comprobantes/')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    fecha = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.usuario.username} - ${self.monto} - {self.estado}"
    
    
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
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    red = models.CharField(max_length=10, choices=REDES)
    txid = models.CharField(max_length=200)
    comprobante = models.ImageField(upload_to='comprobantes_usdt/')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    fecha = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.monto} USDT - {self.estado}"


class Movimiento(models.Model):
    TIPO_CHOICES = [
        ('deposito', 'Depósito'),
        ('retiro', 'Retiro'),
        ('compra', 'Compra'),
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
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    saldo_antes = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    saldo_despues = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    admin_responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acciones_admin'
    )
    fecha = models.DateTimeField(auto_now_add=True)
    descripcion = models.TextField(blank=True, null=True)

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

    def __str__(self):
        return f"{self.moneda} - Compra: {self.compra} / Venta: {self.venta}"

class RetiroARS(models.Model):
    ESTADOS = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('enviado', 'Enviado'),
        ('rechazado', 'Reachazado'),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    alias = models.CharField(max_length=50)
    cbu = models.CharField(max_length=30, blank=True, null=True)
    banco = models.CharField(max_length=50, blank=True, null=True)
    monto = models.DecimalField(max_digits=20, decimal_places=2)
    estado = models.CharField(max_length=20, choices=ESTADOS, default='pendiente')
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)


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
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    direccion_wallet = models.CharField(max_length=255)
    estado = models.CharField(max_length=20, choices=[('pendiente', 'Pendiente'), ('enviado', 'Enviado'), ('rechazado', 'Rechazado')], default='pendiente')
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    admin_responsable =models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='retiros_cripto_aprobados')

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
    adjunto   = models.FileField(upload_to="tickets/", blank=True, null=True)
    estado    = models.CharField(max_length=20, default="abierto")
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-creado_en",)

    def __str__(self):
        return f"#{self.id} {self.asunto}"        
    


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