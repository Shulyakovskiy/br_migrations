TABLE = "django_api_guard.view_history"


def table_exists(cursor):
    cursor.execute("SELECT pg_catalog.to_regclass(%s) IS NOT NULL", [TABLE])
    return cursor.fetchone()[0]


def take_history(cursor, migration_key):
    if not table_exists(cursor):
        return {}
    delete_history(cursor, migration_key)
    return {name: (before, after) for name, before, after in cursor.fetchall()}


def delete_history(cursor, migration_key):
    cursor.execute(
        f"""
        DELETE FROM {TABLE}
        WHERE app_label = %s AND migration_name = %s
        RETURNING view_name, before_query, after_query
        """,
        migration_key,
    )


def record_history(cursor, migration_key, original, restored):
    records = []
    for name, view in restored.items():
        previous = original.get(name)
        before = previous.definition if previous is not None else None
        if before != view.definition:
            records.append((*migration_key, name, before, view.definition))

    if not table_exists(cursor):
        if not records:
            return
        cursor.execute("SELECT pg_catalog.to_regnamespace('django_api_guard') IS NULL")
        if cursor.fetchone()[0]:
            cursor.execute("CREATE SCHEMA django_api_guard")
        cursor.execute(
            f"""
            CREATE TABLE {TABLE} (
                app_label text NOT NULL,
                migration_name text NOT NULL,
                view_name text NOT NULL,
                before_query text,
                after_query text NOT NULL,
                PRIMARY KEY (app_label, migration_name, view_name)
            )
            """
        )

    delete_history(cursor, migration_key)
    if records:
        cursor.executemany(
            f"""
            INSERT INTO {TABLE} (
                app_label, migration_name, view_name, before_query, after_query
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            records,
        )
