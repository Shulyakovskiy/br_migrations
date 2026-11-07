from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.management import get_commands


@register(Tags.compatibility)
def check_migrate_command(app_configs, **kwargs):
    if not getattr(settings, "BR_MIGRATIONS_ENABLED", True):
        return []
    if get_commands().get("migrate") == "br_migrations":
        return []
    return [
        Error(
            "Another application overrides br_migrations' migrate command.",
            hint="Place br_migrations before other applications in INSTALLED_APPS.",
            id="br_migrations.E001",
        )
    ]
