from django.apps import AppConfig


class BrMigrationsConfig(AppConfig):
    name = "br_migrations"
    verbose_name = "BeetRoute migrations"

    def ready(self):
        from . import checks  # Register Django system checks.
