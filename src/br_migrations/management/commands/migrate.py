from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrateCommand
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from br_migrations.execution import ViewDependencies
from br_migrations.history import take_history
from br_migrations.views import catalog_cursor
from br_migrations.views import finish_migration, snapshot_views


class Command(DjangoMigrateCommand):
    _guard_connection = None
    _view_state = None
    _dependencies = None
    _history = None

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
                cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(17318, 20801)")

            self._guard_connection = connection
            self._dependencies = ViewDependencies(connection)
            try:
                with connection.execute_wrapper(self._dependencies):
                    return super().handle(*args, **options)
            finally:
                self._guard_connection = None
                self._view_state = None
                self._dependencies = None
                self._history = None

    def migration_progress_callback(self, action, migration=None, fake=False):
        connection = self._guard_connection
        if connection is not None:
            if action in ("apply_start", "unapply_start") and not fake:
                if not migration.atomic:
                    raise CommandError(f"{migration} must be atomic on this database.")
                self._view_state = snapshot_views(connection)
                self._dependencies.replacements.clear()
                self._history = {}
                if action == "unapply_start":
                    with catalog_cursor(connection) as cursor:
                        self._history = take_history(cursor, (migration.app_label, migration.name))
                self._dependencies.queries = {
                    name: before
                    for name, (before, after) in self._history.items()
                    if before is not None and name in self._view_state
                    and self._view_state[name].definition == after
                }
            if action in ("apply_success", "unapply_success"):
                if self._view_state is not None and not fake:
                    finish_migration(
                        connection,
                        self._view_state,
                        (migration.app_label, migration.name),
                        backwards=action == "unapply_success",
                        replacements=self._dependencies.replacements,
                        history=self._history,
                    )
                self._view_state = None
                if not fake:
                    # PostgreSQL coalesces identical notifications at commit.
                    with connection.cursor() as cursor:
                        cursor.execute("NOTIFY pgrst, 'reload schema'")

        super().migration_progress_callback(action, migration, fake)
