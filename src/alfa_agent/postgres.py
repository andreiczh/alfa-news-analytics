"""Bounded PostgreSQL connections for the runtime store.

The packaged read-only case index and in-memory search index remain SQLite.
Only a small, explicit runtime SQL subset is translated during migration.
"""

from collections.abc import Mapping
import re
import os


class Record(Mapping):
    def __init__(self, names, values):
        self.names, self.values = names, values
        self.positions = {name: i for i, name in enumerate(names)}

    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.positions[key]]

    def __iter__(self):
        return iter(self.names)

    def __len__(self):
        return len(self.names)


def records(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(names, values)


def translate(sql, parameters=False):
    sql = sql.strip()
    if sql.upper().startswith("PRAGMA "):
        return "SELECT 1"
    if sql.upper() == "BEGIN IMMEDIATE":
        # Short admission/lease/idempotency sections. Never held during HTTP or LLM IO.
        return "SELECT pg_advisory_xact_lock(22461025)"
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
    sql = re.sub(
        r"json_extract\((\w+),\s*'\$\.([a-zA-Z0-9_.]+)'\)",
        lambda m: "(" + m[1] + "::jsonb #>> '{" + m[2].replace(".", ",") + "}')",
        sql,
    )
    sql = sql.replace(
        "sqlite_master",
        "(SELECT table_name AS name, 'table' AS type FROM information_schema.tables WHERE table_schema=current_schema()) AS runtime_tables",
    )
    if re.search(r"\bINSERT OR IGNORE\b", sql, re.I):
        sql = re.sub(r"\bINSERT OR IGNORE\b", "INSERT", sql, flags=re.I) + " ON CONFLICT DO NOTHING"
    # A literal question mark in a SQL string must stay a literal. Values are always bound.
    parts = re.split(r"('(?:''|[^'])*')", sql)
    if parameters:
        sql = "".join(
            part.replace("%", "%%").replace("?", "%s") if i % 2 == 0 else part.replace("%", "%%")
            for i, part in enumerate(parts)
        )
    return sql


class Connection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, sql, params=None):
        return self.raw.execute(translate(sql, params is not None), params)

    def executescript(self, script):
        self.raw.execute("SELECT pg_advisory_xact_lock(22461026)")
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)


def create_pool(url, schema="public"):
    import certifi
    from psycopg_pool import ConnectionPool

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", schema):
        raise ValueError("invalid_database_schema")
    return ConnectionPool(
        conninfo=url,
        kwargs={
            "row_factory": records,
            "connect_timeout": 10,
            "sslmode": "verify-full",
            "sslrootcert": os.environ.get("ALFA_DATABASE_CA_FILE") or certifi.where(),
            "options": f"-c search_path={schema} -c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=15000",
        },
        min_size=1,
        max_size=5,
        timeout=10,
        max_waiting=40,
        num_workers=1,
        name="alfa-runtime",
        open=True,
    )
