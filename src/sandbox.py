"""Dedicated, verified test-schema bootstrap and CRUD operations."""

from __future__ import annotations

import hashlib
import random
import re
import time
from typing import Any, Callable

import pymysql

from src.config import AppConfig, ServerConfig
from src.database import connect, quote_identifier
from src.logger import TrafficLogger


MARKER_TABLE = "_mysql_traffic_generator_owner"
DATA_TABLE = "traffic_test_data"
MARKER_KEY = "schema_version"
SCHEMA_VERSION = "mysql-traffic-generator-test-schema-v1"


class TrafficStopped(Exception):
    """Raised when graceful shutdown interrupts a paced sandbox statement."""


def sandbox_database_name(config: AppConfig, server: ServerConfig) -> str:
    identity = f"{server.name}\0{server.host}\0{server.port}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9_]+", "_", server.name.lower()).strip("_")[:16] or "server"
    return f"{config.test_sandbox.database_prefix}{slug}_{digest}"[:64]


def _execute(
    connection: pymysql.connections.Connection,
    logger: TrafficLogger,
    *,
    server: ServerConfig,
    database: str,
    table: str,
    query_type: str,
    sql: str,
    params: tuple[Any, ...] = (),
    fetch: bool = False,
    worker: int = 0,
    before_query: Callable[[], bool] | None = None,
    after_query: Callable[[], bool] | None = None,
    stats: Any = None,
) -> list[tuple[Any, ...]]:
    if before_query is not None and not before_query():
        raise TrafficStopped
    started = time.perf_counter()
    rows: list[tuple[Any, ...]] = []
    error: str | None = None
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                if fetch:
                    rows = list(cursor.fetchall())
        except pymysql.MySQLError as exc:
            error = logger.redact_error(exc)
            duration_ms = (time.perf_counter() - started) * 1000
            logger.query(
                worker=worker,
                server=server.name,
                database=database,
                table=table,
                query_type=query_type,
                sql=sql,
                params=params,
                duration_ms=duration_ms,
                rows=0,
                success=False,
                error=error,
            )
            if stats is not None:
                stats.record(f"{database}.{table}", query_type, duration_ms, 0, False)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        logger.query(
            worker=worker,
            server=server.name,
            database=database,
            table=table,
            query_type=query_type,
            sql=sql,
            params=params,
            duration_ms=duration_ms,
            rows=len(rows) if fetch else 0,
            success=True,
            error=None,
        )
        if stats is not None:
            stats.record(f"{database}.{table}", query_type, duration_ms, len(rows) if fetch else 0, True)
    finally:
        if after_query is not None:
            after_query()
    return rows


def _create_data_table(
    connection: pymysql.connections.Connection,
    logger: TrafficLogger,
    server: ServerConfig,
    database: str,
) -> None:
    _execute(
        connection,
        logger,
        server=server,
        database=database,
        table=DATA_TABLE,
        query_type="sandbox_create_table",
        sql=(
            f"CREATE TABLE {quote_identifier(DATA_TABLE)} ("
            "`id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "`token` VARCHAR(64) NOT NULL, "
            "`payload` VARCHAR(255) NOT NULL, "
            "`created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "KEY `idx_created_at` (`created_at`)"
            ") ENGINE=InnoDB"
        ),
    )


def _validate_data_table(
    connection: pymysql.connections.Connection,
    logger: TrafficLogger,
    server: ServerConfig,
    database: str,
) -> bool:
    rows = _execute(
        connection,
        logger,
        server=server,
        database="information_schema",
        table="COLUMNS",
        query_type="sandbox_validate_table",
        sql=(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, EXTRA "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
        ),
        params=(database, DATA_TABLE),
        fetch=True,
    )
    if not rows:
        return False
    if [str(row[0]) for row in rows] != ["id", "token", "payload", "created_at"]:
        raise RuntimeError(f"Refusing to use unexpected table structure in {database}.{DATA_TABLE}")
    id_row, token_row, payload_row, created_row = rows
    valid = (
        str(id_row[1]).lower().startswith("bigint")
        and str(id_row[2]).upper() == "PRI"
        and "auto_increment" in str(id_row[3]).lower()
        and str(token_row[1]).lower().startswith("varchar(64)")
        and str(payload_row[1]).lower().startswith("varchar(255)")
        and str(created_row[1]).lower().startswith("timestamp")
    )
    if not valid:
        raise RuntimeError(f"Refusing to use unexpected table structure in {database}.{DATA_TABLE}")
    return True


def initialize_sandbox(
    config: AppConfig,
    server: ServerConfig,
    logger: TrafficLogger,
) -> str:
    """Create or verify this server's reserved schema and seed synthetic rows."""
    database = sandbox_database_name(config, server)
    quoted_database = quote_identifier(database)
    identity_hash = hashlib.sha256(f"{server.name}\0{server.host}\0{server.port}".encode("utf-8")).hexdigest()
    marker_value = f"{SCHEMA_VERSION}:{identity_hash}"
    connection = connect(config, server)
    try:
        exists = _execute(
            connection,
            logger,
            server=server,
            database="information_schema",
            table="SCHEMATA",
            query_type="sandbox_check_schema",
            sql="SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
            params=(database,),
            fetch=True,
        )
        is_new = not exists
        if is_new:
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table="-",
                query_type="sandbox_create_database",
                sql=f"CREATE DATABASE {quoted_database}",
            )
        connection.close()
        connection = connect(config, server, database=database)

        if is_new:
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=MARKER_TABLE,
                query_type="sandbox_create_marker_table",
                sql=(
                    f"CREATE TABLE {quote_identifier(MARKER_TABLE)} ("
                    "`marker_key` VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "`marker_value` VARCHAR(255) NOT NULL"
                    ") ENGINE=InnoDB"
                ),
            )
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=MARKER_TABLE,
                query_type="sandbox_register_schema",
                sql="START TRANSACTION",
            )
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=MARKER_TABLE,
                query_type="sandbox_register_schema",
                sql=f"INSERT INTO {quote_identifier(MARKER_TABLE)} (`marker_key`, `marker_value`) VALUES (%s, %s)",
                params=(MARKER_KEY, marker_value),
            )
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=MARKER_TABLE,
                query_type="sandbox_register_schema",
                sql="COMMIT",
            )
            _create_data_table(connection, logger, server, database)
        else:
            marker_rows = _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=MARKER_TABLE,
                query_type="sandbox_verify_schema",
                sql=(
                    f"SELECT `marker_value` FROM {quote_identifier(MARKER_TABLE)} "
                    "WHERE `marker_key` = %s LIMIT 1"
                ),
                params=(MARKER_KEY,),
                fetch=True,
            )
            if not marker_rows or str(marker_rows[0][0]) != marker_value:
                raise RuntimeError(
                    f"Refusing to write to existing database {database}: "
                    "its test-sandbox ownership marker is missing or does not match this server"
                )
            if not _validate_data_table(connection, logger, server, database):
                _create_data_table(connection, logger, server, database)

        if not _validate_data_table(connection, logger, server, database):
            raise RuntimeError(f"Could not verify {database}.{DATA_TABLE}")
        count_rows = _execute(
            connection,
            logger,
            server=server,
            database=database,
            table=DATA_TABLE,
            query_type="sandbox_seed_check",
            sql=f"SELECT COUNT(*) FROM {quote_identifier(DATA_TABLE)}",
            fetch=True,
        )
        if count_rows and int(count_rows[0][0]) == 0:
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=DATA_TABLE,
                query_type="sandbox_seed_transaction",
                sql="START TRANSACTION",
            )
            for index in range(config.test_sandbox.seed_rows):
                _execute(
                    connection,
                    logger,
                    server=server,
                    database=database,
                    table=DATA_TABLE,
                    query_type="sandbox_seed_insert",
                    sql=(
                        f"INSERT INTO {quote_identifier(DATA_TABLE)} (`token`, `payload`) "
                        "VALUES (%s, %s)"
                    ),
                    params=(f"seed-{index + 1:04d}", f"Synthetic seed row {index + 1}"),
                )
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=DATA_TABLE,
                query_type="sandbox_seed_transaction",
                sql="COMMIT",
            )
    finally:
        if connection is not None:
            connection.close()
    return database


def perform_random_operation(
    config: AppConfig,
    server: ServerConfig,
    database: str,
    logger: TrafficLogger,
    *,
    worker: int,
    stats: Any,
    before_query: Callable[[], bool] | None = None,
    after_query: Callable[[], bool] | None = None,
) -> None:
    """Run one random CRUD action after verifying the exact reserved schema."""
    expected_database = sandbox_database_name(config, server)
    if database != expected_database or not database.startswith(config.test_sandbox.database_prefix):
        raise RuntimeError("Refusing test CRUD outside the configured test sandbox")
    action = random.choices(
        list(config.test_sandbox.operation_weights),
        weights=list(config.test_sandbox.operation_weights.values()),
        k=1,
    )[0]
    connection = connect(config, server, database=database)
    try:
        marker_rows = _execute(
            connection,
            logger,
            server=server,
            database=database,
            table=MARKER_TABLE,
            query_type="sandbox_runtime_verify",
            sql=(
                f"SELECT `marker_value` FROM {quote_identifier(MARKER_TABLE)} "
                "WHERE `marker_key` = %s LIMIT 1"
            ),
            params=(MARKER_KEY,),
            fetch=True,
            worker=worker,
            before_query=before_query,
            after_query=after_query,
            stats=stats,
        )
        expected_hash = hashlib.sha256(f"{server.name}\0{server.host}\0{server.port}".encode("utf-8")).hexdigest()
        if not marker_rows or str(marker_rows[0][0]) != f"{SCHEMA_VERSION}:{expected_hash}":
            raise RuntimeError(f"Test-sandbox ownership check failed for {database}; CRUD was stopped")
        data_table = f"{quote_identifier(database)}.{quote_identifier(DATA_TABLE)}"
        sampled_rows = _execute(
            connection,
            logger,
            server=server,
            database=database,
            table=DATA_TABLE,
            query_type="sandbox_row_sample",
            sql=f"SELECT `id` FROM {data_table} ORDER BY `id` DESC LIMIT 100",
            fetch=True,
            worker=worker,
            before_query=before_query,
            after_query=after_query,
            stats=stats,
        )
        candidates = [int(row[0]) for row in sampled_rows]
        if action in {"update", "delete", "select"} and not candidates:
            action = "insert"
        row_id = random.choice(candidates) if candidates else None
        token = f"worker-{worker}-{time.time_ns()}"
        payload = f"Synthetic payload {random.randint(1, 1_000_000)}"
        statements = {
            "select": (f"SELECT `id`, `token`, `payload`, `created_at` FROM {data_table} WHERE `id` = %s LIMIT 1", (row_id,)),
            "insert": (f"INSERT INTO {data_table} (`token`, `payload`) VALUES (%s, %s)", (token, payload)),
            "update": (f"UPDATE {data_table} SET `payload` = %s WHERE `id` = %s LIMIT 1", (payload, row_id)),
            "delete": (f"DELETE FROM {data_table} WHERE `id` = %s LIMIT 1", (row_id,)),
        }
        sql, params = statements[action]
        if sql.split(maxsplit=1)[0].upper() != action.upper() or data_table not in sql or ";" in sql:
            raise RuntimeError("Refusing a test operation outside the fixed CRUD statement allowlist")
        started = time.perf_counter()
        success = False
        error: str | None = None
        rows = 0
        transaction_started = False
        try:
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=DATA_TABLE,
                query_type="sandbox_transaction_begin",
                sql="START TRANSACTION",
                worker=worker,
                before_query=before_query,
                after_query=after_query,
            )
            transaction_started = True
            if before_query is not None and not before_query():
                raise TrafficStopped
            action_started = time.perf_counter()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    rows = cursor.rowcount
            finally:
                action_duration_ms = (time.perf_counter() - action_started) * 1000
                if after_query is not None:
                    after_query()
            _execute(
                connection,
                logger,
                server=server,
                database=database,
                table=DATA_TABLE,
                query_type="sandbox_transaction_commit",
                sql="COMMIT",
                worker=worker,
                before_query=before_query,
                after_query=after_query,
            )
            transaction_started = False
            success = True
        except TrafficStopped:
            if transaction_started:
                try:
                    _execute(
                        connection,
                        logger,
                        server=server,
                        database=database,
                        table=DATA_TABLE,
                        query_type="sandbox_transaction_rollback",
                        sql="ROLLBACK",
                        worker=worker,
                    )
                except pymysql.MySQLError:
                    pass
            raise
        except pymysql.MySQLError as exc:
            error = logger.redact_error(exc)
            if transaction_started:
                try:
                    _execute(
                        connection,
                        logger,
                        server=server,
                        database=database,
                        table=DATA_TABLE,
                        query_type="sandbox_transaction_rollback",
                        sql="ROLLBACK",
                        worker=worker,
                    )
                except pymysql.MySQLError:
                    pass
        duration_ms = (time.perf_counter() - started) * 1000
        logger.query(
            worker=worker,
            server=server.name,
            database=database,
            table=DATA_TABLE,
            query_type=f"sandbox_{action}",
            sql=sql,
            params=params,
            duration_ms=duration_ms,
            rows=rows if success else 0,
            success=success,
            error=error,
        )
        stats.record(
            f"{database}.{DATA_TABLE}",
            f"sandbox_{action}",
            action_duration_ms if success else duration_ms,
            rows,
            success,
        )
    finally:
        connection.close()
