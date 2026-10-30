from django.core.management.base import BaseCommand

from pointage.services import notify_missing_checkouts


class Command(BaseCommand):
    help = "Notifie (in-app + email) les employés ayant oublié de pointer leur départ un jour précédent."

    def handle(self, *args, **options):
        count = notify_missing_checkouts()
        self.stdout.write(self.style.SUCCESS(f"{count} notification(s) envoyée(s)."))
