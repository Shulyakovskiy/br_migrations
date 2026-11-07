import os


SECRET_KEY = "local-br-migrations-tests"
INSTALLED_APPS = ["br_migrations", "tests.locations"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
BR_MIGRATIONS_DATABASE = "default"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("PGDATABASE", "br_migrations_demo"),
        "USER": os.getenv("PGUSER", "postgres"),
        "PASSWORD": os.getenv("PGPASSWORD", "local-demo-password"),
        "HOST": os.getenv("PGHOST", "127.0.0.1"),
        "PORT": os.getenv("PGPORT", "55433"),
    },
}
