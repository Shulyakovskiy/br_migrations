from copy import deepcopy

from django.db import connections
from django.test.runner import DiscoverRunner
from testcontainers.postgres import PostgresContainer


class PostgresTestRunner(DiscoverRunner):
    def run_tests(self, test_labels, extra_tests=None, **kwargs):
        with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
            connection = connections["default"]
            original = deepcopy(connection.settings_dict)
            connection.close()
            connection.settings_dict.update(
                NAME=postgres.dbname,
                USER=postgres.username,
                PASSWORD=postgres.password,
                HOST=postgres.get_container_host_ip(),
                PORT=postgres.get_exposed_port(5432),
            )
            try:
                return super().run_tests(test_labels, extra_tests=extra_tests, **kwargs)
            finally:
                connection.close()
                connection.settings_dict.clear()
                connection.settings_dict.update(original)
