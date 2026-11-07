from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrateCommand
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from br_migrations.views import detach_views, restore_views


class Command(DjangoMigrateCommand):
    _guard_connection = None
    _view_state = None

    def handle(self, *args, **options):
        alias = options["database"]
        if (
            not getattr(settings, "BR_MIGRATIONS_ENABLED", True)
            or alias != getattr(settings, "BR_MIGRATIONS_DATABASE", DEFAULT_DB_ALIAS)
            or options["plan"]
            or options["check_unapplied"]
        ):
            return super().handle(*args, **options)

        connection = connections[alias]
        if connection.vendor != "postgresql":
            raise CommandError("API view migration support requires PostgreSQL.")
        if not connection.get_autocommit():
            raise CommandError("Run migrate outside an existing transaction.")

        with transaction.atomic(using=alias, durable=True):
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                cursor.execute("SELECT pg_catalog.pg_try_advisory_xact_lock(17318, 20801)")
                if not cursor.fetchone()[0]:
                    raise CommandError("Another migration is running on this database.")

            self._guard_connection = connection
            try:
                return super().handle(*args, **options)
            finally:
                self._guard_connection = None
                self._view_state = None

    def migration_progress_callback(self, action, migration=None, fake=False):
        connection = self._guard_connection
        if connection is not None:
            if action in ("apply_start", "unapply_start") and not fake:
                if not migration.atomic:
                    raise CommandError(f"{migration} must be atomic on this database.")
                self._view_state = detach_views(connection)
            if action in ("apply_success", "unapply_success"):
                if self._view_state is not None:
                    migration_key = (migration.app_label, migration.name)
                    restore_views(
                        connection,
                        *self._view_state,
                        migration_key=None if fake else migration_key,
                        backwards=action == "unapply_success",
                    )
                    self._view_state = None
                if not fake:
                    # PostgreSQL coalesces identical notifications at commit.
                    with connection.cursor() as cursor:
                        cursor.execute("NOTIFY pgrst, 'reload schema'")

        super().migration_progress_callback(action, migration, fake)
