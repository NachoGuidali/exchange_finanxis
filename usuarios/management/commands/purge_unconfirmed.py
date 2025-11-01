from django.core.management.base import BaseCommand
from django.utils.timezone import now, timedelta
from django.contrib.auth import get_user_model

class Command(BaseCommand):
    help = "Borra usuarios no confirmados con más de N días"

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, **opts):
        User = get_user_model()
        cutoff = now() - timedelta(days=opts['days'])
        qs = User.objects.filter(email_confirmed=False, date_joined__lt=cutoff)
        n = qs.count()
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Eliminados {n} usuarios no confirmados"))
