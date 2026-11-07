SECRET_KEY = "local-br-migrations-tests"
INSTALLED_APPS = ["br_migrations", "tests.locations"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
BR_MIGRATIONS_DATABASE = "default"
TEST_RUNNER = "tests.runner.PostgresTestRunner"
DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql"}}
