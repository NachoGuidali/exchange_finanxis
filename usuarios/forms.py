from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Usuario, DepositoARS, DepositoUSDT
from django.core.validators import RegexValidator
from .validators import validar_tamano, validar_imagen
from datetime import date, timedelta
from django.conf import settings
from django.utils import timezone
from .models import DeclaracionJurada, Pais, Provincia, Localidad, SupportTicket
from django.contrib.auth.forms import AuthenticationForm
from .models import ExchangeConfig

class ExchangeConfigForm(forms.ModelForm):
    class Meta:
        model = ExchangeConfig
        fields = ("spread_bps_usdt","spread_bps_usd","swap_fee_bps",
                  "umbral_alerta_ars","limite_mensual_operado_ars","limite_diario_retiro_ars")
        widgets = {
            "spread_bps_usdt": forms.NumberInput(attrs={"min":0, "step":1, "class": "campo num", "data-bps": ""}),
            "spread_bps_usd":  forms.NumberInput(attrs={"min":0, "step":1, "class": "campo num", "data-bps": ""}),
            "swap_fee_bps":    forms.NumberInput(attrs={"min":0, "step":1, "class": "campo num", "data-bps": ""}),
            "umbral_alerta_ars":          forms.NumberInput(attrs={"min":0, "step":"0.01", "class": "campo num", "data-pesos": ""}),
            "limite_mensual_operado_ars": forms.NumberInput(attrs={"min":0, "step":"0.01", "class": "campo num", "data-pesos": ""}),
            "limite_diario_retiro_ars":   forms.NumberInput(attrs={"min":0, "step":"0.01", "class": "campo num", "data-pesos": ""}),
        }


DOC_TIPOS = (('DNI','DNI'), ('PAS','Pasaporte'), ('CE','Cédula/CE'))

telefono_val = RegexValidator(
    regex=r'^\+?\d{7,15}$',
    message="Teléfono inválido. Usá solo dígitos, opcional '+' al inicio (7–15)."
)
dni_val = RegexValidator(
    regex=r'^\d{6,12}$',
    message="Número de documento inválido (solo dígitos, 6–12)."
)

class RegistroUsuarioForm(UserCreationForm):
    # Credenciales
    username  = forms.CharField(label="Usuario", required=True)
    email     = forms.EmailField(label="Email", required=True)

    # Identidad
    first_name = forms.CharField(label="Nombre", required=True)
    last_name  = forms.CharField(label="Apellido", required=True)
    persona_tipo = forms.ChoiceField(label="Tipo de persona", choices=Usuario._meta.get_field("persona_tipo").choices, initial="FISICA")
    doc_tipo = forms.ChoiceField(
    label="Tipo de documento",
    choices=DOC_TIPOS,
    initial='DNI'
)
    doc_nro    = forms.CharField(label="N° de documento", required=True, validators=[dni_val])

    estado_civil = forms.ChoiceField(label="Estado civil", choices=Usuario._meta.get_field("estado_civil").choices, required=False)
    sexo         = forms.ChoiceField(label="Sexo", choices=Usuario._meta.get_field("sexo").choices, required=False)
    nacionalidad = forms.CharField(label="Nacionalidad", required=True)
    fecha_nacimiento = forms.DateField(
        label="Fecha de nacimiento",
        required=True,
        widget=forms.DateInput(attrs={"type": "date"})
    )
    lugar_nacimiento = forms.CharField(label="Lugar de nacimiento", required=True)

    # Contacto
    telefono  = forms.CharField(label="Teléfono", required=True, validators=[telefono_val])

    # Domicilio
    pais      = forms.ModelChoiceField(label="País", queryset=Pais.objects.all(), required=True)
    provincia = forms.ModelChoiceField(label="Provincia", queryset=Provincia.objects.none(), required=True)
    localidad = forms.ModelChoiceField(label="Localidad", queryset=Localidad.objects.none(), required=True)
    codigo_postal = forms.CharField(label="Código postal", required=True, max_length=16)
    calle         = forms.CharField(label="Calle", required=True, max_length=128)
    numero_calle  = forms.CharField(label="Número", required=True, max_length=16)
    piso          = forms.CharField(label="Piso", required=False, max_length=16)
    depto         = forms.CharField(label="Depto", required=False, max_length=16)

    # KYC imágenes
    dni_frente = forms.ImageField(label="DNI (frente)", required=True, validators=[validar_tamano(5), validar_imagen])
    dni_dorso  = forms.ImageField(label="DNI (dorso)",  required=True, validators=[validar_tamano(5), validar_imagen])

    acepta_tyc = forms.BooleanField(label="Acepto los Términos y Condiciones", required=True)

    class Meta(UserCreationForm.Meta):
        model = Usuario
        fields = (
            "username", "email", "password1", "password2",
            "first_name", "last_name", "persona_tipo",
            "doc_tipo", "doc_nro",
            "estado_civil", "sexo", "nacionalidad", "fecha_nacimiento", "lugar_nacimiento",
            "telefono",
            "pais", "provincia", "localidad", "codigo_postal", "calle", "numero_calle", "piso", "depto",
            "dni_frente", "dni_dorso",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Estilos (clases de static/css/finanxis.css)
        base = "campo"
        select_base = "campo pr-8"
        file_base = "campo-archivo"
        checkbox_base = "casilla"

        for name, field in self.fields.items():
            classes = field.widget.attrs.get("class", "")
            if field.widget.__class__.__name__ in ("Select","SelectMultiple"):
                field.widget.attrs["class"] = f"{classes} {select_base}".strip()
            elif field.widget.__class__.__name__ in ("ClearableFileInput","FileInput"):
                field.widget.attrs["class"] = f"{classes} {file_base}".strip()
            elif field.widget.__class__.__name__ in ("CheckboxInput",):
                field.widget.attrs["class"] = f"{classes} {checkbox_base}".strip()
            else:
                field.widget.attrs["class"] = f"{classes} {base}".strip()

        # Cascada dependiente
        pais_val = self.data.get("pais") or (self.instance.pais_id if self.instance.pk else None)
        prov_val = self.data.get("provincia") or (self.instance.provincia_id if self.instance.pk else None)
        loc_val  = self.data.get("localidad") or (self.instance.localidad_id if self.instance.pk else None)

        # Provincias
        if pais_val:
            self.fields["provincia"].queryset = Provincia.objects.filter(pais_id=pais_val).order_by("nombre")
        elif prov_val:
            self.fields["provincia"].queryset = Provincia.objects.filter(id=prov_val)
        else:
            self.fields["provincia"].queryset = Provincia.objects.none()

        # Localidades
        if prov_val:
            self.fields["localidad"].queryset = Localidad.objects.filter(provincia_id=prov_val).order_by("nombre")
        elif loc_val:
            self.fields["localidad"].queryset = Localidad.objects.filter(id=loc_val)
        else:
            self.fields["localidad"].queryset = Localidad.objects.none()

        # Autocomplete
        self.fields["username"].widget.attrs["autocomplete"] = "username"
        self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.fields["password1"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password2"].widget.attrs["autocomplete"] = "new-password"

    # 📧 Validar email
    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and Usuario.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Este email ya está registrado.")
        return email

    # 🎂 Validar fecha de nacimiento
    def clean_fecha_nacimiento(self):
        fn = self.cleaned_data["fecha_nacimiento"]
        hoy = date.today()
        mayoria = date(hoy.year - 18, hoy.month, hoy.day)
        if fn > mayoria:
            raise forms.ValidationError("Debés ser mayor de 18 años.")
        if fn < hoy - timedelta(days=120*365):
            raise forms.ValidationError("Fecha de nacimiento inválida.")
        return fn

    # 🧭 Validar coherencia país/provincia/localidad
    def clean(self):
        cleaned = super().clean()
        pais = cleaned.get("pais")
        prov = cleaned.get("provincia")
        loc  = cleaned.get("localidad")

        if prov and pais and prov.pais_id != pais.id:
            self.add_error("provincia", "La provincia no corresponde al país seleccionado.")
        if loc and prov and loc.provincia_id != prov.id:
            self.add_error("localidad", "La localidad no corresponde a la provincia seleccionada.")

        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.estado_verificacion = 'pendiente'
        user.is_active = True

        # Campos adicionales
        user.doc_tipo = self.cleaned_data["doc_tipo"]
        user.doc_nro  = self.cleaned_data["doc_nro"]
        user.persona_tipo = self.cleaned_data["persona_tipo"]
        user.estado_civil = self.cleaned_data.get("estado_civil", "")
        user.sexo         = self.cleaned_data.get("sexo", "")
        user.nacionalidad = self.cleaned_data["nacionalidad"]
        user.fecha_nacimiento = self.cleaned_data["fecha_nacimiento"]
        user.lugar_nacimiento = self.cleaned_data["lugar_nacimiento"]
        user.telefono = self.cleaned_data["telefono"]

        # Domicilio
        user.pais = self.cleaned_data["pais"]
        user.provincia = self.cleaned_data["provincia"]
        user.localidad = self.cleaned_data["localidad"]
        user.codigo_postal = self.cleaned_data["codigo_postal"]
        user.calle         = self.cleaned_data["calle"]
        user.numero_calle  = self.cleaned_data["numero_calle"]
        user.piso          = self.cleaned_data.get("piso", "")
        user.depto         = self.cleaned_data.get("depto", "")

        # Documentos
        user.dni_frente = self.cleaned_data["dni_frente"]
        user.dni_dorso  = self.cleaned_data["dni_dorso"]

        # Términos y condiciones
        if self.cleaned_data.get("acepta_tyc"):
            user.tyc_aceptado = True
            user.tyc_version = getattr(settings, "TYC_VERSION", "")
            user.tyc_aceptado_en = timezone.now()

        if commit:
            user.save()
        return user


def _clean_monto_positivo(form):
    monto = form.cleaned_data.get('monto')
    if monto is None or monto <= 0:
        raise forms.ValidationError("El monto debe ser mayor a 0.")
    return monto


class DepositoARSForm(forms.ModelForm):
    class Meta:
        model = DepositoARS
        fields = ['monto', 'comprobante']
        widgets = {
            'monto': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'campo'}),
            'comprobante': forms.ClearableFileInput(attrs={'class': 'campo-archivo'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['comprobante'].validators += [validar_tamano(5), validar_imagen]

    def clean_monto(self):
        return _clean_monto_positivo(self)

class DepositoUSDTForm(forms.ModelForm):
    class Meta:
        model = DepositoUSDT
        fields = ['monto', 'red', 'txid', 'comprobante']
        widgets = {
            'monto': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'campo'}),
            'red': forms.Select(attrs={'class': 'campo'}),
            'txid': forms.TextInput(attrs={'class': 'campo'}),
            'comprobante': forms.ClearableFileInput(attrs={'class': 'campo-archivo'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['comprobante'].validators += [validar_tamano(5), validar_imagen]

    def clean_monto(self):
        return _clean_monto_positivo(self)

    def clean_txid(self):
        txid = (self.cleaned_data.get('txid') or '').strip()
        if DepositoUSDT.objects.filter(txid__iexact=txid).exclude(estado='rechazado').exists():
            raise forms.ValidationError("Ese TXID ya fue informado en otro depósito.")
        return txid

def validar_adjunto_ticket(file):
    permitidos = ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')
    if getattr(file, 'content_type', '') not in permitidos:
        raise forms.ValidationError("Adjunto inválido (JPG/PNG/WebP/PDF).")
    if not file.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.pdf')):
        raise forms.ValidationError("Extensión de archivo no permitida.")


class SupportTicketForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['adjunto'].validators += [validar_tamano(5), validar_adjunto_ticket]

    class Meta:
        model = SupportTicket
        fields = ["email","asunto","categoria","prioridad","mensaje","adjunto"]
        widgets = {
            "email":     forms.EmailInput(attrs={"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
            "asunto":    forms.TextInput(attrs={"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
            "categoria": forms.Select(attrs={"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
            "prioridad": forms.Select(attrs={"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
            "mensaje":   forms.Textarea(attrs={"rows":5,"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
            "adjunto":   forms.ClearableFileInput(attrs={"class":"w-full rounded-md border border-white/10 bg-white/90 text-black px-3 py-2"}),
        }        


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label="Email o usuario",
        widget=forms.TextInput(attrs={"autofocus": True, "placeholder": "tu@email.com o usuario"})
    )        

class DeclaracionJuradaForm(forms.ModelForm):
    SI_NO = [('', 'Elegí una opción'), ('no', 'No'), ('si', 'Sí')]

    es_pep = forms.TypedChoiceField(choices=SI_NO, coerce=lambda v: v == 'si',
                                    label="¿Sos Persona Expuesta Políticamente (PEP)?")
    es_sujeto_obligado = forms.TypedChoiceField(choices=SI_NO, coerce=lambda v: v == 'si',
                                                label="¿Sos Sujeto Obligado ante la UIF?")
    residencia_fiscal_exterior = forms.TypedChoiceField(choices=SI_NO, coerce=lambda v: v == 'si',
                                                        label="¿Tenés residencia fiscal fuera de Argentina?")
    declara_veracidad = forms.BooleanField(
        required=True,
        label="Declaro bajo juramento que los datos son correctos y completos, y me comprometo a "
              "informar cualquier cambio dentro de los 30 días.",
        error_messages={'required': "Tenés que confirmar la declaración para continuar."},
    )

    class Meta:
        model = DeclaracionJurada
        fields = [
            'es_pep', 'pep_tipo', 'pep_cargo', 'pep_organismo',
            'es_sujeto_obligado', 'sujeto_obligado_actividad',
            'actividad', 'origen_fondos', 'origen_detalle', 'ingresos_mensuales', 'volumen_mensual_previsto',
            'residencia_fiscal_exterior', 'residencia_fiscal_pais', 'residencia_fiscal_nif',
            'declara_veracidad',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # al actualizar, los sí/no vienen del modelo como booleanos
        for campo in ('es_pep', 'es_sujeto_obligado', 'residencia_fiscal_exterior'):
            valor = self.initial.get(campo)
            if isinstance(valor, bool):
                self.initial[campo] = 'si' if valor else 'no'
        self.initial['declara_veracidad'] = False

    def clean(self):
        d = super().clean()
        requeridos = []
        if d.get('es_pep'):
            requeridos += ['pep_tipo', 'pep_cargo', 'pep_organismo']
        if d.get('es_sujeto_obligado'):
            requeridos += ['sujeto_obligado_actividad']
        if d.get('origen_fondos') == 'otro':
            requeridos += ['origen_detalle']
        if d.get('residencia_fiscal_exterior'):
            requeridos += ['residencia_fiscal_pais', 'residencia_fiscal_nif']
        for campo in requeridos:
            if not (d.get(campo) or '').strip():
                self.add_error(campo, "Completá este dato.")
        # lo que no aplica no se guarda
        if not d.get('es_pep'):
            d['pep_tipo'] = d['pep_cargo'] = d['pep_organismo'] = ''
        if not d.get('es_sujeto_obligado'):
            d['sujeto_obligado_actividad'] = ''
        if not d.get('residencia_fiscal_exterior'):
            d['residencia_fiscal_pais'] = d['residencia_fiscal_nif'] = ''
        return d
