from dataclasses import dataclass

from django.core.management.base import CommandError


@dataclass(frozen=True)
class Grant:
    grantor: str
    grantee: str
    privileges: str
    grantable: bool


def read_acl(cursor, name):
    cursor.execute(
        """
        WITH entries AS (
            SELECT 0 AS position, NULL::text AS column_name,
                   COALESCE(relacl, acldefault('r', relowner)) AS acl
            FROM pg_class WHERE oid = %s::regclass
            UNION ALL
            SELECT attnum, quote_ident(attname), attacl
            FROM pg_attribute
            WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
        )
        SELECT quote_ident(pg_get_userbyid(g.grantor)),
               CASE WHEN g.grantee = 0 THEN 'PUBLIC'
                    ELSE quote_ident(pg_get_userbyid(g.grantee)) END,
               string_agg(g.privilege_type || COALESCE(' (' || column_name || ')', ''),
                          ', ' ORDER BY g.privilege_type),
               g.is_grantable
        FROM entries
        CROSS JOIN LATERAL aclexplode(acl) WITH ORDINALITY
            AS g(grantor, grantee, privilege_type, is_grantable, grant_order)
        GROUP BY position, column_name, g.grantor, g.grantee, g.is_grantable
        ORDER BY position, min(g.grant_order)
        """,
        [name, name],
    )
    return [Grant(*row) for row in cursor.fetchall()]


def restore_acl(cursor, name, owner, grants):
    defaults = read_acl(cursor, name)
    if set(defaults) == set(grants):
        return

    cursor.execute("SHOW role")
    (original_role,) = cursor.fetchone()
    role = None
    if defaults:
        grantees = ", ".join(sorted({grant.grantee for grant in defaults}))
        cursor.execute(f"SET LOCAL ROLE {owner}")
        role = owner
        cursor.execute(f"REVOKE ALL PRIVILEGES ON TABLE {name} FROM {grantees} CASCADE")

    # The catalog ACL order preserves grants that later grantors rely on.
    for grant in grants:
        if role != grant.grantor:
            cursor.execute(f"SET LOCAL ROLE {grant.grantor}")
            role = grant.grantor
        grant_option = " WITH GRANT OPTION" if grant.grantable else ""
        cursor.execute(
            f"GRANT {grant.privileges} ON TABLE {name} TO {grant.grantee}{grant_option}"
        )

    cursor.execute("SELECT set_config('role', %s, true)", [original_role])
    if set(read_acl(cursor, name)) != set(grants):
        raise CommandError(f"Could not reproduce the original grants on {name}.")


def read_columns(cursor, name):
    cursor.execute(
        """
        SELECT attname, atttypid::regtype::text, atttypmod, attcollation
        FROM pg_attribute
        WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
        ORDER BY attnum
        """,
        [name],
    )
    return cursor.fetchall()


def rebuild_view(cursor, name, create_sql):
    cursor.execute(
        """
        SELECT quote_ident(pg_get_userbyid(relowner)),
               quote_literal(obj_description(oid, 'pg_class')),
               EXISTS (SELECT FROM pg_trigger WHERE tgrelid = v.oid)
               OR EXISTS (
                   SELECT FROM pg_rewrite WHERE ev_class = v.oid AND rulename <> '_RETURN'
               )
               OR EXISTS (
                   SELECT FROM pg_seclabel WHERE classoid = 'pg_class'::regclass
                                             AND objoid = v.oid
               )
        FROM pg_class AS v WHERE oid = %s::regclass
        """,
        [name],
    )
    owner, comment, unsupported = cursor.fetchone()
    if unsupported:
        raise CommandError(f"Cannot rebuild {name} with triggers, extra rules or security labels.")

    columns = read_columns(cursor, name)
    grants = read_acl(cursor, name)
    cursor.execute(
        """
        SELECT quote_ident(a.attname),
               quote_literal(col_description(a.attrelid, a.attnum)),
               pg_get_expr(d.adbin, d.adrelid)
        FROM pg_attribute AS a
        LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        [name],
    )
    attributes = cursor.fetchall()

    cursor.execute(f"DROP VIEW {name} RESTRICT")
    cursor.execute(create_sql)

    rebuilt = read_columns(cursor, name)
    if len(rebuilt) != len(columns) or any(
        old != new
        and (
            old[0] != new[0]
            or old[3] != new[3]
            or {old[1], new[1]} != {"character varying", "text"}
        )
        for old, new in zip(columns, rebuilt)
    ):
        raise CommandError(f"Only varchar/text type changes are supported when rebuilding {name}.")

    for column, column_comment, default in attributes:
        if column_comment is not None:
            cursor.execute(f"COMMENT ON COLUMN {name}.{column} IS {column_comment}")
        if default is not None:
            cursor.execute(f"ALTER VIEW {name} ALTER COLUMN {column} SET DEFAULT {default}")
    if comment is not None:
        cursor.execute(f"COMMENT ON VIEW {name} IS {comment}")
    cursor.execute(f"ALTER VIEW {name} OWNER TO {owner}")
    restore_acl(cursor, name, owner, grants)
