from contextlib import contextmanager
from dataclasses import dataclass
from graphlib import TopologicalSorter

from django.core.management.base import CommandError
from django.db import ProgrammingError, transaction

from br_migrations.history import record_history, take_history
from br_migrations.rebuild import rebuild_view


@dataclass(frozen=True)
class View:
    oid: int
    definition: str
    projection: str
    options: str | None


@contextmanager
def catalog_cursor(connection):
    with connection.cursor() as cursor:
        cursor.execute("SHOW search_path")
        (search_path,) = cursor.fetchone()
        # Deparse references to application objects with schema-qualified names.
        cursor.execute("SET LOCAL search_path TO pg_catalog, pg_temp")
        yield cursor
        cursor.execute("SELECT set_config('search_path', %s, true)", [search_path])


def read_views(cursor):
    cursor.execute(
        """
        SELECT v.oid,
               format('%I.%I', n.nspname, v.relname),
               pg_get_viewdef(v.oid),
               (
                   SELECT string_agg(
                       format('CAST(NULL AS %s)%s AS %I',
                           format_type(a.atttypid, a.atttypmod),
                           CASE WHEN c.oid IS NULL THEN '' ELSE
                               format(' COLLATE %I.%I', cn.nspname, c.collname)
                           END,
                           a.attname),
                       ', ' ORDER BY a.attnum
                   )
                   FROM pg_attribute AS a
                   LEFT JOIN pg_collation AS c ON c.oid = a.attcollation
                   LEFT JOIN pg_namespace AS cn ON cn.oid = c.collnamespace
                   WHERE a.attrelid = v.oid AND a.attnum > 0 AND NOT a.attisdropped
               ),
               (
                   SELECT string_agg(format('%I = %L', option_name, option_value),
                                     ', ' ORDER BY option_name)
                   FROM pg_options_to_table(v.reloptions)
               )
        FROM pg_class AS v
        JOIN pg_namespace AS n ON n.oid = v.relnamespace
        WHERE n.nspname = 'api' AND v.relkind = 'v'
        ORDER BY v.relname
        """
    )
    return {
        name: View(oid, definition, projection or "", options)
        for oid, name, definition, projection, options in cursor.fetchall()
    }


def detach_views(connection):
    with catalog_cursor(connection) as cursor:
        original = order_views(cursor, read_views(cursor))
        for name, view in original.items():
            # If migration code queries this view, fail instead of returning fake data.
            cursor.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT {view.projection} "
                "FROM pg_catalog.current_setting('br_migrations.view_is_detached')"
            )
        detached = read_views(cursor) if original else {}
        if any(
            name not in detached or detached[name].oid != view.oid
            for name, view in original.items()
        ):
            raise CommandError("A view in schema api changed while being detached.")
    return original, detached


def order_views(cursor, views):
    if not views:
        return views

    by_oid = {view.oid: name for name, view in views.items()}
    graph = TopologicalSorter({oid: () for oid in by_oid})
    cursor.execute(
        """
        SELECT r.ev_class, COALESCE(t.typrelid, d.refobjid)
        FROM pg_rewrite AS r
        JOIN pg_depend AS d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
        LEFT JOIN pg_type AS t ON d.refclassid = 'pg_type'::regclass AND t.oid = d.refobjid
        WHERE r.rulename = '_RETURN' AND r.ev_class = ANY(%s::oid[])
          AND d.refclassid IN ('pg_class'::regclass, 'pg_type'::regclass)
        """,
        [list(by_oid)],
    )
    for dependent, dependency in cursor.fetchall():
        if dependency in by_oid and dependent != dependency:
            graph.add(dependent, dependency)
    ordered = (by_oid[oid] for oid in graph.static_order())
    return {name: views[name] for name in ordered}


def restore_views(connection, original, detached, migration_key, backwards):
    with catalog_cursor(connection) as cursor:
        current = read_views(cursor)
        history = take_history(cursor, migration_key) if backwards else {}
        to_restore = {}
        for name, view in original.items():
            before, after = history.get(name, (None, None))
            if name not in current and before is None and after == view.definition:
                # Only allow the inverse of CREATE VIEW, including its original identity.
                cursor.execute("SELECT 1 FROM pg_class WHERE oid = %s", [view.oid])
                if cursor.fetchone() is None:
                    continue
            if current.get(name) != detached[name]:
                raise CommandError(
                    f"{name} was changed by the migration. "
                    "Run intentional changes to API views separately."
                )
            query = view.definition
            if before is not None and after == view.definition:
                query = before
            to_restore[name] = query

        for name, query in to_restore.items():
            options = original[name].options
            with_options = f" WITH ({options})" if options else ""
            definition = f"VIEW {name}{with_options} AS {query}"
            try:
                with transaction.atomic(using=connection.alias):
                    cursor.execute(f"CREATE OR REPLACE {definition}")
            except ProgrammingError as exc:
                cause = exc.__cause__
                if getattr(cause, "sqlstate", getattr(cause, "pgcode", None)) != "42P16":
                    raise
                rebuild_view(cursor, name, f"CREATE {definition}")

        restored = read_views(cursor) if to_restore else current
        for name in to_restore:
            view = original[name]
            actual = restored[name]
            if actual.options != view.options or (
                actual.oid == view.oid and actual.projection != view.projection
            ):
                raise CommandError(f"The public signature or options of {name} changed.")

        if migration_key is not None and not backwards:
            record_history(cursor, migration_key, original, restored)
