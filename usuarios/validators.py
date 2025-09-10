from django.core.exceptions import ValidationError

def validar_tamano(max_mb=5):
    def _v(file):
        if file.size > max_mb * 1024 * 1024:
            raise ValidationError(f"Archivo mayor a {max_mb}MB.")
    return _v

def validar_imagen(file):
    valid = ('image/jpeg', 'image/png', 'image/webp')
    if getattr(file, 'content_type', '') not in valid:
        raise ValidationError("Formato inválido (JPG/PNG/WebP).")
