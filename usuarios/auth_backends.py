from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

class EmailOrUsernameBackend(ModelBackend):
    """
    Permite login con username o email (case-insensitive) y usa la
    lógica estándar de ModelBackend para contraseñas y is_active.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)

        if username is None or password is None:
            return None

        # Buscar por username o email, ambos case-insensitive.
        qs = UserModel._default_manager.filter(
            Q(username__iexact=username) | Q(email__iexact=username)
        ).order_by('-is_active')  # si hay duplicados, preferí activos

        for user in qs:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None