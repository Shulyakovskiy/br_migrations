from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Event
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, NotSupportedError, connection, connections, migrations, models
from django.test import TransactionTestCase, override_settings


class ApiViewMigrationTests(TransactionTestCase):
    def setUp(self):
        self.migrate("0002")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO public.locations_location (title) VALUES ('Station')"
            )

    def migrate(self, target):
        call_command("migrate", "locations", target, stdout=StringIO(), verbosity=0)

    def view_metadata(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.oid, c.relowner,
                       COALESCE(c.relacl, acldefault('r', c.relowner))::text,
                       ARRAY(SELECT option FROM unnest(c.reloptions) AS option ORDER BY option),
                       obj_description(c.oid, 'pg_class'),
                       array_agg(col_description(c.oid, a.attnum) ORDER BY a.attnum),
                       array_agg(a.attacl::text ORDER BY a.attnum)
                FROM pg_class AS c
                JOIN pg_attribute AS a ON a.attrelid = c.oid
                WHERE c.oid = %s::regclass AND a.attnum > 0
                GROUP BY c.oid
                """,
                [name],
            )
            return cursor.fetchone()

    def table_title_type(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT format_type(atttypid, atttypmod)
                FROM pg_attribute
                WHERE attrelid = 'public.locations_location'::regclass
                  AND attname = 'title'
                """
            )
            return cursor.fetchone()[0]

    def test_forward_and_reverse_preserve_existing_acl_and_owner(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE ROLE demo_view_owner;
                CREATE ROLE demo_view_reader;
                CREATE ROLE demo_view_delegate;
                CREATE ROLE demo_view_default;
                """
            )
            self.addCleanup(self.remove_test_roles)
            cursor.execute("GRANT USAGE, CREATE ON SCHEMA api TO demo_view_owner")
            cursor.execute("GRANT USAGE ON SCHEMA api TO demo_view_reader, demo_view_delegate")
            cursor.execute("GRANT SELECT ON public.locations_location TO demo_view_owner")
            cursor.execute("ALTER VIEW api.location_cards OWNER TO demo_view_owner")
            cursor.execute("SET ROLE demo_view_owner")
            try:
                cursor.execute(
                    "GRANT SELECT ON api.location_cards TO demo_view_reader "
                    "WITH GRANT OPTION"
                )
                cursor.execute(
                    "GRANT SELECT (title), UPDATE (title) "
                    "ON api.location_cards TO demo_view_reader "
                    "WITH GRANT OPTION"
                )
                cursor.execute("GRANT SELECT (title) ON api.location_cards TO PUBLIC")
                cursor.execute("ALTER VIEW api.location_cards SET (check_option = local)")
                cursor.execute("COMMENT ON VIEW api.location_cards IS 'Location cards'")
                cursor.execute("COMMENT ON COLUMN api.location_cards.title IS 'Public title'")
            finally:
                cursor.execute("RESET ROLE")
            cursor.execute("SET ROLE demo_view_reader")
            try:
                cursor.execute("GRANT SELECT ON api.location_cards TO demo_view_delegate")
                cursor.execute(
                    "GRANT SELECT (title) ON api.location_cards TO demo_view_delegate "
                    "WITH GRANT OPTION"
                )
            finally:
                cursor.execute("RESET ROLE")
            cursor.execute(
                """
                ALTER DEFAULT PRIVILEGES IN SCHEMA api
                    GRANT SELECT ON TABLES TO demo_view_default;
                CREATE VIEW api.a_new_locations AS
                    SELECT l.title::text AS title FROM public.locations_location AS l
                    JOIN api.location_titles AS t ON t.title = l.title
                """
            )
            self.addCleanup(self.drop_new_locations_view)

        cards = self.view_metadata("api.location_cards")
        titles = self.view_metadata("api.location_titles")
        new_view = self.view_metadata("api.a_new_locations")

        for target, expected_type in (
            ("0003", "text"),
            ("0002", "character varying"),
            ("0003", "text"),
        ):
            self.migrate(target)
            self.assertEqual(self.table_title_type(), expected_type)
            rebuilt_cards = self.view_metadata("api.location_cards")
            rebuilt_titles = self.view_metadata("api.location_titles")
            self.assertNotEqual(rebuilt_cards[0], cards[0])
            self.assertNotEqual(rebuilt_titles[0], titles[0])
            self.assertEqual(rebuilt_cards[1:], cards[1:])
            self.assertEqual(rebuilt_titles[1:], titles[1:])
            cards, titles = rebuilt_cards, rebuilt_titles
            self.assertEqual(self.view_metadata("api.a_new_locations"), new_view)
            with connection.cursor() as cursor:
                cursor.execute("SELECT title, pg_typeof(title)::text FROM api.location_titles")
                self.assertEqual(cursor.fetchall(), [("Station", expected_type)])
                cursor.execute("SELECT title, pg_typeof(title)::text FROM api.a_new_locations")
                self.assertEqual(cursor.fetchall(), [("Station", "text")])
                cursor.execute("SET ROLE demo_view_delegate")
                try:
                    cursor.execute("SELECT title FROM api.location_cards")
                    self.assertEqual(cursor.fetchall(), [("Station",)])
                finally:
                    cursor.execute("RESET ROLE")

    def drop_new_locations_view(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW api.a_new_locations")

    def remove_test_roles(self):
        with connection.cursor() as cursor:
            cursor.execute("ALTER VIEW api.location_cards OWNER TO CURRENT_USER")
            roles = "demo_view_owner, demo_view_reader, demo_view_delegate, demo_view_default"
            cursor.execute(f"DROP OWNED BY {roles}")
            cursor.execute(f"DROP ROLE {roles}")

    def test_external_dependency_rolls_back_table_and_migration_record(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE VIEW public.external_cards AS SELECT title FROM api.location_cards"
            )
            self.addCleanup(self.drop_external_view)
            cursor.execute("GRANT SELECT ON api.location_cards TO PUBLIC")
        original = self.view_metadata("api.location_cards")

        with self.assertRaisesRegex(DatabaseError, "other objects depend on it"):
            self.migrate("0003")

        self.assertEqual(self.table_title_type(), "character varying")
        self.assertEqual(self.view_metadata("api.location_cards"), original)
        with connection.cursor() as cursor:
            cursor.execute("SELECT title FROM public.external_cards")
            self.assertEqual(cursor.fetchall(), [("Station",)])
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM django_migrations
                    WHERE app = 'locations' AND name = '0003_location_title_text'
                )
                """
            )
            self.assertFalse(cursor.fetchone()[0])
        self.drop_external_view()
        self.migrate("0003")
        self.assertEqual(self.table_title_type(), "text")

    def drop_external_view(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW IF EXISTS public.external_cards")

    def test_reverse_of_view_creation_removes_only_the_views_created_by_it(self):
        self.migrate("0001")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT to_regclass('api.location_cards'), to_regclass('api.location_titles')
                """
            )
            self.assertEqual(cursor.fetchone(), (None, None))
        self.migrate("0003")
        with connection.cursor() as cursor:
            cursor.execute("SELECT title FROM api.location_cards")
            self.assertEqual(cursor.fetchall(), [("Station",)])

    def test_reverse_cannot_replace_a_view_under_the_same_name(self):
        with connection.cursor() as cursor:
            cursor.execute("GRANT SELECT ON api.location_cards TO PUBLIC")
        original = self.view_metadata("api.location_cards")

        migration = import_module("tests.locations.migrations.0002_api_views").Migration
        operation = migration.operations[0]
        replacement = migrations.RunSQL(
            operation.sql,
            reverse_sql=operation.reverse_sql + """
                CREATE VIEW api.location_cards AS
                    SELECT id, title FROM public.locations_location;
            """,
        )
        with patch.object(migration, "operations", [replacement]):
            with self.assertRaisesMessage(CommandError, "api.location_cards was changed"):
                self.migrate("0001")

        self.assertEqual(self.view_metadata("api.location_cards"), original)
        # The failed rollback also rolled back deletion of the journal entries.
        self.migrate("0001")
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('api.location_cards')")
            self.assertIsNone(cursor.fetchone()[0])

    @override_settings(BR_MIGRATIONS_ENABLED=False)
    def test_disabled_uses_the_native_django_migration(self):
        with self.assertRaisesRegex(
            NotSupportedError, "cannot alter type of a column used by a view or rule"
        ):
            self.migrate("0003")
        self.assertEqual(self.table_title_type(), "character varying")

    def test_repeated_migrate_preserves_views_and_journal(self):
        self.migrate("0003")
        original = self.view_metadata("api.location_cards")
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM django_api_guard.view_history ORDER BY 1, 2, 3")
            history = cursor.fetchall()
        for _ in range(2):
            call_command("migrate", stdout=StringIO(), verbosity=0)
            self.assertEqual(self.view_metadata("api.location_cards"), original)
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM django_api_guard.view_history ORDER BY 1, 2, 3")
                self.assertEqual(cursor.fetchall(), history)

    def test_rename_and_type_change_preserve_view_queries_in_both_directions(self):
        migration = import_module("tests.locations.migrations.0003_location_title_text").Migration
        operations = [
            migrations.RenameField(model_name="location", old_name="title", new_name="name"),
            migrations.AlterField(model_name="location", name="name", field=models.TextField()),
        ]
        with patch.object(migration, "operations", operations):
            self.migrate("0003")
            with connection.cursor() as cursor:
                cursor.execute("SELECT title, pg_typeof(title)::text FROM api.location_cards")
                self.assertEqual(cursor.fetchall(), [("Station", "text")])
            self.migrate("0002")
            with connection.cursor() as cursor:
                cursor.execute("SELECT title FROM api.location_cards")
                self.assertEqual(cursor.fetchall(), [("Station",)])

    def test_new_view_before_type_change_is_discovered(self):
        migration = import_module("tests.locations.migrations.0003_location_title_text").Migration
        operations = [
            migrations.RunSQL(
                "CREATE VIEW api.new_cards AS SELECT title FROM api.location_cards",
                "DROP VIEW api.new_cards",
            ),
            *migration.operations,
        ]
        with patch.object(migration, "operations", operations):
            self.migrate("0003")
            with connection.cursor() as cursor:
                cursor.execute("SELECT title, pg_typeof(title)::text FROM api.new_cards")
                self.assertEqual(cursor.fetchall(), [("Station", "text")])
            self.migrate("0002")

    def test_concurrent_migrate_serializes_and_rechecks_the_plan(self):
        migration = import_module("tests.locations.migrations.0003_location_title_text").Migration
        started = Event()

        def hold_transaction(apps, schema_editor):
            started.set()
            schema_editor.execute("SELECT pg_sleep(0.5)")

        def migrate_in_thread():
            try:
                self.migrate("0003")
            finally:
                connections.close_all()

        operations = [migrations.RunPython(hold_transaction, migrations.RunPython.noop),
                      *migration.operations]
        with patch.object(migration, "operations", operations):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(migrate_in_thread)
                self.assertTrue(started.wait(5))
                second = pool.submit(migrate_in_thread)
                first.result(timeout=10)
                second.result(timeout=10)
        self.assertEqual(self.table_title_type(), "text")

    def test_model_rename_and_type_change_use_native_dependencies(self):
        migration = import_module("tests.locations.migrations.0003_location_title_text").Migration
        operations = [
            migrations.RenameModel(old_name="Location", new_name="Place"),
            migrations.AlterField(model_name="place", name="title", field=models.TextField()),
        ]
        with patch.object(migration, "operations", operations):
            self.migrate("0003")
            with connection.cursor() as cursor:
                cursor.execute("SELECT title, pg_typeof(title)::text FROM api.location_cards")
                self.assertEqual(cursor.fetchall(), [("Station", "text")])
            self.migrate("0002")
        self.assertEqual(self.table_title_type(), "character varying")

    def test_data_migration_can_read_views_before_and_after_type_change(self):
        migration = import_module("tests.locations.migrations.0003_location_title_text").Migration
        values = []

        def read_view(apps, schema_editor):
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("SELECT title FROM api.location_cards")
                values.append(cursor.fetchall())

        operations = [
            migrations.RunPython(read_view, migrations.RunPython.noop),
            *migration.operations,
            migrations.RunPython(read_view, migrations.RunPython.noop),
        ]
        with patch.object(migration, "operations", operations):
            self.migrate("0003")
            self.migrate("0002")
        self.assertEqual(values, [[("Station",)], [("Station",)]])
