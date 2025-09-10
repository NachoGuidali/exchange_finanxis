from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Usuario, DepositoARS, DepositoUSDT
from django.core.validators import RegexValidator
from .validators import validar_tamano, validar_imagen


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
    # Identidad
    first_name = forms.CharField(label="Nombre", required=True)
    last_name  = forms.CharField(label="Apellido", required=True)
    doc_tipo   = forms.ChoiceField(label="Tipo de documento", choices=DOC_TIPOS, required=True, initial='DNI')
    doc_nro    = forms.CharField(label="N° de documento", required=True, validators=[dni_val])
    nacionalidad = forms.CharField(label="Nacionalidad", required=False)

    # Contacto / domicilio
    email     = forms.EmailField(label="Email", required=True)
    telefono  = forms.CharField(label="Teléfono", required=True, validators=[telefono_val])
    domicilio = forms.CharField(label="Domicilio", required=True, max_length=255)

    # KYC imágenes (OBLIGATORIO)
    dni_frente = forms.ImageField(
        label="DNI (frente)", required=True,
        validators=[validar_tamano(5), validar_imagen]
    )
    dni_dorso = forms.ImageField(
        label="DNI (dorso)", required=True,
        validators=[validar_tamano(5), validar_imagen]
    )

    class Meta(UserCreationForm.Meta):
        model = Usuario
        fields = (
            # credenciales
            "username", "password1", "password2",
            # identidad/contacto
            "first_name", "last_name", "email", "telefono", "domicilio",
            "doc_tipo", "doc_nro", "nacionalidad",
            # kyc
            "dni_frente", "dni_dorso",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        base = "w-full rounded-md border border-ink-200 px-3 py-2"
        for name, field in self.fields.items():
            classes = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{classes} {base}".strip()

        # Autocomplete y hints útiles
        self.fields["username"].widget.attrs["autocomplete"] = "username"
        self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.fields["password1"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password2"].widget.attrs["autocomplete"] = "new-password"
        self.fields["telefono"].widget.attrs["placeholder"] = "+54911XXXXYYYY"

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and Usuario.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Este email ya está registrado.")
        return email

    def clean(self):
        cleaned = super().clean()
        # redundante pero explícito
        if not cleaned.get("dni_frente") or not cleaned.get("dni_dorso"):
            raise forms.ValidationError("Subí ambas fotos del DNI (frente y dorso).")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.estado_verificacion = 'pendiente'  # queda bloqueado para operar
        user.is_active = True                   # puede loguearse y ver “en verificación”
        # set de campos extra (Meta ya los mapea, pero por claridad):
        user.doc_tipo = self.cleaned_data["doc_tipo"]
        user.doc_nro = self.cleaned_data["doc_nro"]
        user.nacionalidad = self.cleaned_data.get("nacionalidad","")
        user.telefono = self.cleaned_data["telefono"]
        user.domicilio = self.cleaned_data["domicilio"]
        user.email = self.cleaned_data["email"]
        user.dni_frente = self.cleaned_data["dni_frente"]
        user.dni_dorso = self.cleaned_data["dni_dorso"]
        if commit:
            user.save()
        return user


class DepositoARSForm(forms.ModelForm):
    class Meta:
        model = DepositoARS
        fields = ['monto', 'comprobante']

class DepositoUSDTForm(forms.ModelForm):
    class Meta:
        model = DepositoUSDT
        fields = ['monto', 'red', 'txid', 'comprobante']
        widgets = {
            'monto': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }