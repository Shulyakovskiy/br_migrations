from io import StringIO

from django.apps import apps
from django.core.checks import Tags, run_checks
from django.core.management import call_command, get_commands
from django.test import SimpleTestCase, override_settings

from br_migrations.apps import BrMigrationsConfig


class InstallationTests(SimpleTestCase):
    def test_short_app_name_loads_config_and_command(self):
        self.assertIsInstance(apps.get_app_config("br_migrations"), BrMigrationsConfig)
        self.assertEqual(get_commands()["migrate"], "br_migrations")
        self.assertEqual(run_checks(tags=[Tags.compatibility]), [])

    @override_settings(INSTALLED_APPS=["tests.locations", "br_migrations"])
    def test_shadowed_command_is_reported(self):
        self.assertEqual(get_commands()["migrate"], "tests.locations")
        errors = run_checks(tags=[Tags.compatibility])
        self.assertEqual([error.id for error in errors], ["br_migrations.E001"])

    @override_settings(
        INSTALLED_APPS=["tests.locations", "br_migrations"],
        BR_MIGRATIONS_ENABLED=False,
    )
    def test_disabled_allows_the_project_migrate_command(self):
        self.assertEqual(run_checks(tags=[Tags.compatibility]), [])


class MigrationStateTests(SimpleTestCase):
    databases = {"default"}

    def test_models_match_migration_state(self):
        call_command("makemigrations", check=True, dry_run=True, stdout=StringIO())
