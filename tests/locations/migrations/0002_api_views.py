from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("locations", "0001_initial")]

    operations = [
        migrations.RunSQL(
            """
            CREATE SCHEMA IF NOT EXISTS api;
            CREATE VIEW api.location_cards WITH (security_barrier = true) AS
                SELECT id, title FROM public.locations_location;
            CREATE VIEW api.location_titles AS SELECT title FROM api.location_cards;
            """,
            """
            DROP VIEW api.location_titles;
            DROP VIEW api.location_cards;
            """,
        ),
    ]
