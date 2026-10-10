"""MySQL connection helpers, metadata inspection, and read-only guard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import pymysql

from src.config import AppConfig, ServerConfig


class _SelectOnlyConnection(pymysql.connections.Connection):
    """PyMySQL connection that skips its implicit charset setup command."""

    def set_character_set(self, charset: str, collation: str | None = None) -> None:
        # PyMySQL normally sends an extra session command after authentication.
        # The collation is carried in the handshake; keep the client encoding
        # from Connection.__init__ and avoid issuing a non-SELECT statement.
        if charset != self.charset:
            raise ValueError("Changing the connection charset would require a non-SELECT statement")
        self.collation = collation


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    column_type: str
    is_primary: bool
    is_indexed: bool


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    primary_keys: tuple[str, ...]
    estimated_rows: int | None
    database: str = ""


def quote_identifier(identifier: str) -> str:
    """Quote a MySQL identifier, escaping embedded backticks."""
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise ValueError("SQL identifier must be a non-empty string without NUL")
    return "`" + identifier.replace("`", "``") + "`"


def validate_sql(sql: str) -> None:
    """Reject anything except one plain SELECT suitable for generated traffic."""
    statement = sql.strip()
    if not statement or not re.match(r"(?is)^SELECT\b", statement):
        raise ValueError("Only SELECT statements are allowed for generated traffic")
    hint = re.match(r"(?is)^SELECT\s*/\*\+\s*MAX_EXECUTION_TIME\(\d+\)\s*\*/", statement)
    checked_statement = "SELECT " + statement[hint.end():] if hint else statement
    without_identifiers = re.sub(r"`(?:``|[^`])*`", " ", checked_statement)
    if ";" in without_identifiers or any(marker in without_identifiers for marker in ("--", "#", "/*", "*/", "@", "'", '"')):
        raise ValueError("Comments, variables, and multiple statements are not allowed")
    unsafe_patterns = (
        r"\bINTO\b",
        r"\b(?:SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK|LOAD_FILE)\s*\(",
        r"\bFOR\s+UPDATE\b",
        r"\bLOCK\s+IN\s+SHARE\s+MODE\b",
    )
    if re.search(r"(?is)" + "|".join(unsafe_patterns), without_identifiers):
        raise ValueError("Unsafe SELECT clause or function rejected")


def add_execution_timeout_hint(sql: str, timeout_ms: int) -> str:
    """Attach a MySQL per-SELECT timeout hint without changing session state."""
    if timeout_ms <= 0:
        return sql
    if not re.match(r"(?is)^SELECT\b", sql.strip()):
        raise ValueError("Execution timeout hints can only be added to SELECT statements")
    return re.sub(
        r"(?is)^SELECT\b",
        f"SELECT /*+ MAX_EXECUTION_TIME({timeout_ms}) */",
        sql,
        count=1,
    )


def connect(
    config: AppConfig,
    server: ServerConfig | None = None,
    *,
    database: str | None = None,
    read_timeout: float | None = None,
) -> pymysql.connections.Connection:
    """Open a connection with an optional read budget for startup metadata."""
    endpoint = server or config.mysql
    selected_database = database if server is not None or database is not None else config.mysql.database
    return _SelectOnlyConnection(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=selected_database,
        charset=endpoint.charset,
        ssl_disabled=endpoint.ssl_disabled,
        program_name="mysql-traffic-generator",
        connect_timeout=endpoint.connect_timeout,
        read_timeout=(
            max(5, config.query.max_execution_time_ms / 1000 + 5)
            if read_timeout is None else read_timeout
        ),
        write_timeout=endpoint.connect_timeout,
        # None preserves the server default and avoids a session setup command.
        autocommit=None,
    )


def list_databases(connection: pymysql.connections.Connection) -> list[str]:
    """Return schemas visible to the current account, excluding MySQL internals."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN (%s, %s, %s, %s) ORDER BY SCHEMA_NAME",
            ("information_schema", "mysql", "performance_schema", "sys"),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def list_database_tables(
    connection: pymysql.connections.Connection,
    excluded_schemas: Sequence[str] = (),
) -> list[tuple[str, str, int | None]]:
    """List base tables without opening every table's storage statistics."""
    excluded = tuple(dict.fromkeys((
        "information_schema", "mysql", "performance_schema", "sys", *excluded_schemas
    )))
    placeholders = ", ".join(["%s"] * len(excluded))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, NULL FROM information_schema.TABLES "
            f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN ({placeholders}) "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME",
            excluded,
        )
        result = []
        for row in cursor.fetchall():
            try:
                estimate = None if row[2] is None else max(0, int(row[2]))
            except (TypeError, ValueError):
                estimate = None
            result.append((str(row[0]), str(row[1]), estimate))
        return result


def list_tables(connection: pymysql.connections.Connection) -> list[str]:
    """Return base tables only, using a read-only information_schema query."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            if len(row) < 2 or str(row[1]).upper() == "BASE TABLE":
                result.append(str(row[0]))
        return result


def table_row_estimates(connection: pymysql.connections.Connection) -> dict[str, int]:
    """Read approximate row counts from information_schema.TABLES."""
    estimates: dict[str, int] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        rows = cursor.fetchall()
    for row in rows:
        try:
            value = row[1]
            if value is not None:
                estimates[str(row[0])] = max(0, int(value))
        except (IndexError, TypeError, ValueError):
            continue
    return estimates


def load_table_info(
    connection: pymysql.connections.Connection,
    table_name: str,
    estimated_rows: int | None = None,
    database: str | None = None,
) -> TableInfo:
    """Inspect columns and indexes using a parameterized read-only SELECT."""
    with connection.cursor() as cursor:
        if database is None:
            cursor.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (table_name,),
            )
        else:
            cursor.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (database, table_name),
            )
        rows = cursor.fetchall()
    columns = tuple(
        ColumnInfo(
            str(row[0]),
            str(row[1]),
            str(row[2]).upper() == "PRI",
            str(row[2]).upper() in {"PRI", "UNI", "MUL"},
        )
        for row in rows
    )
    primary = tuple(column.name for column in columns if column.is_primary)
    if estimated_rows is None:
        with connection.cursor() as cursor:
            schema = "DATABASE()" if database is None else "%s"
            params = (table_name,) if database is None else (database, table_name)
            cursor.execute(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = {schema} AND TABLE_NAME = %s LIMIT 1",
                params,
            )
            estimate = cursor.fetchone()
        if estimate and estimate[0] is not None:
            try:
                estimated_rows = max(0, int(estimate[0]))
            except (TypeError, ValueError):
                pass
    return TableInfo(table_name, columns, primary, estimated_rows, database or "")
