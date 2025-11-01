from django.contrib.auth.tokens import PasswordResetTokenGenerator

class EmailActivationTokenGenerator(PasswordResetTokenGenerator):
    pass

email_activation_token = EmailActivationTokenGenerator()
