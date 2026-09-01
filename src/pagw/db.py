from contextlib import contextmanager
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pagw.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.dsn = settings.database_url.get_secret_value()

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout = '10s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            yield connection

    def migrate(self):
        with self.transaction() as c:
            c.execute("SELECT pg_advisory_xact_lock(472192)")
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version int PRIMARY KEY)")
            if not c.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                c.execute(files("pagw").joinpath("schema.sql").read_text())
                c.execute("INSERT INTO schema_migrations VALUES (1)")
            if not c.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
                c.execute(files("pagw").joinpath("schema_v2.sql").read_text())
                c.execute("INSERT INTO schema_migrations VALUES (2)")
            if not c.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
                c.execute(files("pagw").joinpath("schema_v3.sql").read_text())
                c.execute("INSERT INTO schema_migrations VALUES (3)")

    def one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.transaction() as c:
            return c.execute(query, params).fetchone()
