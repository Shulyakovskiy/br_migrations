import sqlparse
from django.db import DatabaseError, transaction

from br_migrations.views import detach_views, restore_views


class ViewDependencies:
    def __init__(self, connection):
        self.connection = connection
        self.replacements = {}
        self.queries = {}
        self.resolving = False

    def __call__(self, execute, sql, params, many, context):
        if self.resolving:
            return execute(sql, params, many, context)
        statement = sqlparse.format(sql, strip_comments=True).lstrip().upper()
        if not statement.startswith("ALTER TABLE"):
            return execute(sql, params, many, context)

        self.resolving = True
        try:
            try:
                with transaction.atomic(using=self.connection.alias):
                    return execute(sql, params, many, context)
            except DatabaseError as exc:
                cause = exc.__cause__
                if getattr(cause, "sqlstate", getattr(cause, "pgcode", None)) != "0A000":
                    raise
                with transaction.atomic(using=self.connection.alias):
                    original, detached = detach_views(self.connection)
                    if not original:
                        raise
                    result = execute(sql, params, many, context)
                    restored = restore_views(self.connection, original, detached, self.queries)
                    self.replacements.update({
                        view.oid: restored[name].oid
                        for name, view in original.items()
                        if restored[name].oid != view.oid
                    })
                    return result
        finally:
            self.resolving = False
