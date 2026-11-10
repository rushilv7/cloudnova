"""Read-only query execution (specs/solution.md section 6).

Second layer after `app.sqlguard`: the database is opened read-only, an
authorizer allows only SELECT over the modeled tables and views with approved
functions, extension loading stays off, and a progress handler enforces a
deadline. A query that slips past the validator still cannot write, attach
another database, read the catalog or call an unapproved function.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import quote

from app.config import Settings

READABLE = {"invoices", "accounts", "invoice_facts", "account_facts"}
# SQLite function names, including those the view definitions use.
APPROVED_FUNCTIONS = {
    "count", "sum", "avg", "min", "max", "coalesce", "ifnull", "nullif", "round", "abs",
    "substr", "substring", "upper", "lower", "length", "instr", "like",
    "strftime", "date", "julianday",
}
_PROGRESS_STEPS = 200


class QueryDenied(Exception):
    """The connection refused the statement (write, catalog access, unapproved function...)."""


class QueryTimeout(Exception):
    """The statement ran past its deadline and was interrupted."""


class QueryFailed(Exception):
    """SQLite could not prepare or run the statement."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool


def _authorize(action, arg1, arg2, _database, _source):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 in READABLE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in APPROVED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class Database:
    """A guarded read-only connection. Use as a context manager."""

    def __init__(self, settings: Settings):
        if not settings.db_path.is_file():
            raise QueryFailed(f"database {settings.db_path} does not exist; run the build first")
        self._settings = settings
        self._deadline = 0.0
        self._conn = sqlite3.connect(f"file:{quote(str(settings.db_path))}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only = ON")
        if hasattr(self._conn, "enable_load_extension"):
            self._conn.enable_load_extension(False)
        self._conn.set_authorizer(_authorize)
        self._conn.set_progress_handler(self._expired, _PROGRESS_STEPS)

    def _expired(self) -> int:
        return 1 if time.monotonic() > self._deadline else 0

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc) -> None:
        self._conn.close()

    def query(self, sql: str, parameters: dict | None = None, row_cap: int | None = None) -> QueryResult:
        """Run one statement. `row_cap` limits the rows returned, never the rows the statement reads."""
        self._deadline = time.monotonic() + self._settings.query_timeout_s
        try:
            cursor = self._conn.execute(sql, parameters or {})
            if row_cap is None:
                rows, truncated = cursor.fetchall(), False
            else:
                rows = cursor.fetchmany(row_cap + 1)
                truncated = len(rows) > row_cap
                rows = rows[:row_cap]
            columns = [d[0] for d in cursor.description] if cursor.description else []
        except sqlite3.Error as exc:
            message = str(exc)
            if "interrupt" in message:
                raise QueryTimeout(
                    f"the query exceeded the {self._settings.query_timeout_s:g}s execution deadline") from None
            if "not authorized" in message or "prohibited" in message or "readonly" in message:
                raise QueryDenied(f"the read-only connection refused the statement: {message}") from None
            raise QueryFailed(message) from None
        return QueryResult(columns, [tuple(row) for row in rows], truncated)


def run_query(settings: Settings, sql: str, parameters: dict | None = None, row_cap: int | None = None) -> QueryResult:
    """Open a guarded connection, run one statement and close it."""
    with Database(settings) as database:
        return database.query(sql, parameters, row_cap)
