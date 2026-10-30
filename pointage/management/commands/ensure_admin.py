import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from pointage.models import Profile


class Command(BaseCommand):
    help = "Creates the first admin account from ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL if it does not exist yet."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        if not username or not password:
            self.stdout.write("ADMIN_USERNAME / ADMIN_PASSWORD not set, skipping.")
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin '{username}' already exists.")
            return
        user = User.objects.create_superuser(username=username, email=os.environ.get("ADMIN_EMAIL", ""), password=password)
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.role = Profile.Role.ADMIN
        profile.save()
        self.stdout.write(self.style.SUCCESS(f"Admin '{username}' created."))
