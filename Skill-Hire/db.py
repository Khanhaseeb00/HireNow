"""Database compatibility layer.

Local development defaults to SQLite. Production can use PostgreSQL by setting
DATABASE_URL. Application code continues to use qmark placeholders so existing
queries do not need a wholesale rewrite.
"""
import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "kaamgar.db"))
DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_MIGRATION_URL") or "").strip()
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
POSTGRES_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema_postgres.sql")


def is_postgres():
    return bool(DATABASE_URL)


def _pg_sql(sql):
    sql = sql.strip()
    if sql.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    # Existing app SQL uses SQLite qmark placeholders. There are no literal
    # question marks in the application's SQL statements.
    return sql.replace("?", "%s")


class _PostgresCursor:
    def __init__(self, cursor, connection):
        self._cursor = cursor
        self._connection = connection

    def execute(self, sql, params=()):
        self._cursor.execute(_pg_sql(sql), params)
        return self

    def executemany(self, sql, params_seq):
        self._cursor.executemany(_pg_sql(sql), params_seq)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    @property
    def lastrowid(self):
        # All application inserts that read lastrowid target SERIAL/BIGSERIAL
        # primary keys. LASTVAL() is connection-local, so concurrent requests
        # cannot steal another request's generated id.
        with self._connection._conn.cursor() as cur:
            cur.execute("SELECT LASTVAL() AS id")
            row = cur.fetchone()
            return row["id"] if isinstance(row, dict) else row[0]

    def close(self):
        self._cursor.close()


class _PostgresConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_pg_sql(sql), params)
        return _PostgresCursor(cur, self)

    def cursor(self):
        return _PostgresCursor(self._conn.cursor(), self)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_db():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return _PostgresConnection(conn)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn, table_name):
    if DATABASE_URL:
        rows = conn.execute(
            """SELECT column_name AS name
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = ?""",
            (table_name,),
        ).fetchall()
        return {row["name"] for row in rows}
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def id_column_sql():
    return "BIGSERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"


def for_update(sql):
    """Add a row lock on PostgreSQL while keeping SQLite syntax valid."""
    return f"{sql} FOR UPDATE" if DATABASE_URL else sql


def init_db():
    """Create tables if they do not exist. Safe to call at startup."""
    conn = get_db()
    schema_path = POSTGRES_SCHEMA_PATH if DATABASE_URL else SCHEMA_PATH
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = f.read()

    if DATABASE_URL:
        # schema files contain ordinary DDL separated by semicolons.
        for statement in [part.strip() for part in schema.split(";") if part.strip()]:
            conn.execute(statement)
    else:
        conn.executescript(schema)
    conn.commit()
    conn.close()


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
