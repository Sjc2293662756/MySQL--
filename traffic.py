"""Command-line entry point for the MySQL multi-server traffic generator."""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pymysql

from src.config import AppConfig, ConfigError, ServerConfig, load_config
from src.database import (
    TableInfo,
    add_execution_timeout_hint,
    connect,
    list_database_tables,
    list_databases,
    load_table_info,
    validate_sql,
)
from src.generator import make_query
from src.logger import TrafficLogger
from src.sandbox import TrafficStopped, initialize_sandbox, perform_random_operation
from src.stats import TrafficStats


METADATA_READ_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class TableRef:
    server: ServerConfig
    database: str
    table: str
    estimated_rows: int | None


@dataclass
class ServerInventory:
    server: ServerConfig
    databases: list[str] = field(default_factory=list)
    tables: list[TableRef] = field(default_factory=list)


class TrafficPacer:
    """Coordinate a process-wide QPS target and optional short bursts."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.burst_until = 0.0

    def _burst_multiplier(self, now: float) -> float:
        burst = self.config.traffic.random_burst
        if not burst.enabled:
            return 1.0
        if now >= self.burst_until and random.random() < burst.probability:
            self.burst_until = now + burst.duration_seconds
        return burst.multiplier if now < self.burst_until else 1.0

    def before_query(self, stop_event: threading.Event) -> bool:
        traffic = self.config.traffic
        if traffic.mode != "qps":
            return not stop_event.is_set()
        with self.lock:
            now = time.monotonic()
            multiplier = self._burst_multiplier(now)
            slot = max(now, self.next_slot)
            self.next_slot = slot + 1.0 / (traffic.qps * multiplier)
        return not stop_event.wait(max(0.0, slot - time.monotonic()))

    def after_query(self, stop_event: threading.Event) -> bool:
        traffic = self.config.traffic
        if traffic.mode != "interval":
            return not stop_event.is_set()
        with self.lock:
            multiplier = self._burst_multiplier(time.monotonic())
        delay = random.uniform(traffic.interval_min, traffic.interval_max) / multiplier
        return not stop_event.wait(delay)


def check_servers(config: AppConfig, logger: TrafficLogger) -> tuple[int, int]:
    """Check each configured endpoint with a SELECT 1 and no schema changes."""
    succeeded = 0
    failed = 0
    for server in config.servers:
        connection = None
        try:
            connection = connect(config, server)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            succeeded += 1
            print(f"Connection OK: {server.name} ({server.host}:{server.port})")
        except pymysql.MySQLError as exc:
            failed += 1
            print(
                f"Connection failed: {server.name} ({server.host}:{server.port}): "
                f"{logger.redact_error(exc)}",
                file=sys.stderr,
            )
        finally:
            if connection is not None:
                connection.close()
    print(f"Server checks: {succeeded} succeeded, {failed} failed")
    return succeeded, failed


def discover_cluster(config: AppConfig, logger: TrafficLogger) -> tuple[dict[str, ServerInventory], int]:
    """Discover visible database names and base tables on every configured server."""
    inventories: dict[str, ServerInventory] = {}
    failed_servers = 0
    for server in config.servers:
        inventory = ServerInventory(server)
        connection = None
        try:
            # Large metadata inventories need a separate budget from normal queries.
            connection = connect(config, server, read_timeout=METADATA_READ_TIMEOUT_SECONDS)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            database_names = list_databases(connection)
            refs = list_database_tables(connection)
            inventory.databases = database_names
            inventory.tables = [TableRef(server, database, table, estimate) for database, table, estimate in refs]
            inventories[server.name] = inventory
            logger.info(
                "server=%s discovered %s visible databases and %s base tables",
                server.name,
                len(inventory.databases),
                len(inventory.tables),
            )
        except pymysql.MySQLError as exc:
            failed_servers += 1
            logger.error("server=%s discovery failed: %s", server.name, logger.redact_error(exc))
            print(f"Discovery failed: {server.name}: {logger.redact_error(exc)}", file=sys.stderr)
        finally:
            if connection is not None:
                connection.close()
    return inventories, failed_servers


def print_cluster_listing(inventories: dict[str, ServerInventory]) -> None:
    total_databases = 0
    total_tables = 0
    for inventory in inventories.values():
        print(f"\nServer: {inventory.server.name} ({inventory.server.host}:{inventory.server.port})")
        print(f"Databases: {len(inventory.databases)}")
        by_database: dict[str, list[str]] = {name: [] for name in inventory.databases}
        for ref in inventory.tables:
            by_database.setdefault(ref.database, []).append(ref.table)
        for database in inventory.databases:
            tables = by_database.get(database, [])
            print(f"  {database}: {len(tables)} base tables")
            for table in tables:
                print(f"    - {table}")
        total_databases += len(inventory.databases)
        total_tables += len(inventory.tables)
    print(f"\nTotal: {len(inventories)} servers, {total_databases} databases, {total_tables} base tables")


def traffic_tables(config: AppConfig, inventory: ServerInventory) -> list[TableRef]:
    excluded = set(config.tables.exclude)
    business_refs = [
        ref for ref in inventory.tables
        if not ref.database.startswith(config.test_sandbox.database_prefix)
    ]
    if config.tables.mode == "manual":
        included = set(config.tables.include)
        return [ref for ref in business_refs if ref.table in included and ref.table not in excluded]
    return [ref for ref in business_refs if ref.table not in excluded]


def weighted_table_ref(config: AppConfig, tables: Sequence[TableRef]) -> TableRef:
    weights = [config.table_weights.get(ref.table, 1.0) for ref in tables]
    if not any(weight > 0 for weight in weights):
        weights = [1.0] * len(tables)
    return random.choices(list(tables), weights=weights, k=1)[0]


def print_cluster_inspection(config: AppConfig, inventories: dict[str, ServerInventory], logger: TrafficLogger) -> None:
    for inventory in inventories.values():
        connection = None
        try:
            connection = connect(config, inventory.server)
            for ref in inventory.tables:
                try:
                    info = load_table_info(connection, ref.table, ref.estimated_rows, ref.database)
                except pymysql.MySQLError as exc:
                    logger.warning(
                        "server=%s cannot inspect %s.%s: %s",
                        inventory.server.name,
                        ref.database,
                        ref.table,
                        logger.redact_error(exc),
                    )
                    continue
                print(f"\n{inventory.server.name}/{info.database}.{info.name}")
                print(f"Estimated rows: {info.estimated_rows if info.estimated_rows is not None else 'unknown'}")
                print("Columns:")
                for column in info.columns:
                    suffix = " PRIMARY KEY" if column.is_primary else ""
                    print(f"  - {column.name}: {column.column_type}{suffix}")
        finally:
            if connection is not None:
                connection.close()


def cluster_worker_main(
    worker_id: int,
    config: AppConfig,
    inventories: dict[str, ServerInventory],
    test_databases: dict[str, str],
    logger: TrafficLogger,
    stats: TrafficStats,
    pacer: TrafficPacer,
    stop_event: threading.Event,
) -> None:
    """Randomly query visible schemas across servers and optionally CRUD the test sandboxes."""
    servers = [inventory.server for inventory in inventories.values()]
    connection_by_server: dict[str, pymysql.connections.Connection] = {}
    retry_counts: dict[str, int] = {}
    failed_until: dict[str, float] = {}
    table_cache: dict[tuple[str, str, str], TableInfo] = {}

    def ensure_connection(server: ServerConfig) -> pymysql.connections.Connection | None:
        connection = connection_by_server.get(server.name)
        if connection is not None and connection.open:
            return connection
        retry_count = retry_counts.get(server.name, 0)
        while not stop_event.is_set():
            wait = failed_until.get(server.name, 0.0) - time.monotonic()
            if wait > 0 and stop_event.wait(wait):
                return None
            try:
                connection = connect(config, server)
                connection_by_server[server.name] = connection
                retry_counts[server.name] = 0
                logger.info("worker=%s connected server=%s", worker_id, server.name)
                return connection
            except pymysql.MySQLError as exc:
                retry_count += 1
                retry_counts[server.name] = retry_count
                maximum = server.reconnect.max_retries
                if not server.reconnect.enabled or (maximum and retry_count > maximum):
                    logger.error(
                        "worker=%s server=%s reconnect stopped after failure: %s",
                        worker_id,
                        server.name,
                        logger.redact_error(exc),
                    )
                    failed_until[server.name] = float("inf")
                    return None
                delay = server.reconnect.interval_seconds
                failed_until[server.name] = time.monotonic() + delay
                logger.warning(
                    "worker=%s server=%s reconnecting in %s seconds: %s",
                    worker_id,
                    server.name,
                    delay,
                    logger.redact_error(exc),
                )
                if stop_event.wait(delay):
                    return None
        return None

    active_server: ServerConfig | None = None
    active_database = ""
    active_table = ""
    connection: pymysql.connections.Connection | None = None
    connection_lost = False
    transaction_active = False

    def execute_select(sql: str, params: tuple[object, ...], query_type: str) -> list[tuple[object, ...]]:
        nonlocal connection, connection_lost
        if not pacer.before_query(stop_event):
            return []
        sql = add_execution_timeout_hint(sql, config.query.max_execution_time_ms)
        started = time.perf_counter()
        rows: list[tuple[object, ...]] = []
        error_text = None
        success = False
        try:
            validate_sql(sql)
            if connection is None:
                raise pymysql.InterfaceError("database connection is unavailable")
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = list(cursor.fetchall())
            success = True
        except (pymysql.MySQLError, ValueError) as exc:
            error_text = logger.redact_error(exc)
            error_code = exc.args[0] if isinstance(exc, pymysql.MySQLError) and exc.args else None
            if isinstance(exc, pymysql.InterfaceError) or error_code in {2006, 2013, 2055}:
                connection_lost = True
                if active_server is not None:
                    try:
                        connection.close() if connection is not None else None
                    except pymysql.MySQLError:
                        pass
                    connection_by_server.pop(active_server.name, None)
                connection = None
        duration_ms = (time.perf_counter() - started) * 1000
        stats.record(f"{active_database}.{active_table}", query_type, duration_ms, len(rows) if success else 0, success)
        logger.query(
            worker=worker_id,
            server=active_server.name if active_server else None,
            database=active_database,
            table=active_table,
            query_type=query_type,
            sql=sql,
            params=params,
            duration_ms=duration_ms,
            rows=len(rows) if success else 0,
            success=success,
            error=error_text,
        )
        pacer.after_query(stop_event)
        return rows

    def transaction_control(statement: str, event_type: str) -> bool:
        nonlocal connection, connection_lost
        allowed = {
            "START TRANSACTION": "transaction_begin",
            "COMMIT": "transaction_commit",
            "ROLLBACK": "transaction_rollback",
        }
        if allowed.get(statement) != event_type:
            raise ValueError("Unsupported transaction control statement")
        started = time.perf_counter()
        error_text = None
        success = False
        try:
            if connection is None:
                raise pymysql.InterfaceError("database connection is unavailable")
            with connection.cursor() as cursor:
                cursor.execute(statement)
            success = True
        except pymysql.MySQLError as exc:
            error_text = logger.redact_error(exc)
            error_code = exc.args[0] if exc.args else None
            if isinstance(exc, pymysql.InterfaceError) or error_code in {2006, 2013, 2055}:
                connection_lost = True
                if active_server is not None:
                    try:
                        connection.close() if connection is not None else None
                    except pymysql.MySQLError:
                        pass
                    connection_by_server.pop(active_server.name, None)
                connection = None
        logger.query(
            worker=worker_id,
            server=active_server.name if active_server else None,
            database=active_database or "-",
            table="-",
            query_type=event_type,
            sql=statement,
            params=(),
            duration_ms=(time.perf_counter() - started) * 1000,
            rows=0,
            success=success,
            error=error_text,
        )
        if success:
            outcome = {"transaction_begin": "started", "transaction_commit": "committed", "transaction_rollback": "rolled_back"}.get(event_type)
            if outcome:
                stats.record_transaction(outcome)
        return success

    def rollback_transaction() -> None:
        nonlocal transaction_active
        if transaction_active:
            if connection is not None and transaction_control("ROLLBACK", "transaction_rollback"):
                transaction_active = False
                return
            stats.record_transaction("aborted")
            logger.warning("worker=%s read-only transaction aborted on server=%s", worker_id, active_server.name if active_server else "?")
            transaction_active = False

    try:
        while not stop_event.is_set():
            usable_servers = [
                server for server in servers
                if traffic_tables(config, inventories[server.name]) or server.name in test_databases
                if failed_until.get(server.name, 0.0) != float("inf")
            ]
            if not usable_servers:
                logger.error("worker=%s has no available server targets; stopped", worker_id)
                break
            active_server = random.choice(usable_servers)
            inventory = inventories[active_server.name]
            use_sandbox = (
                active_server.name in test_databases
                and (
                    not traffic_tables(config, inventory)
                    or random.random() < config.test_sandbox.traffic_probability
                )
            )
            if use_sandbox:
                try:
                    perform_random_operation(
                        config,
                        active_server,
                        test_databases[active_server.name],
                        logger,
                        worker=worker_id,
                        stats=stats,
                        before_query=lambda: pacer.before_query(stop_event),
                        after_query=lambda: pacer.after_query(stop_event),
                    )
                except TrafficStopped:
                    break
                except (pymysql.MySQLError, RuntimeError) as exc:
                    logger.error(
                        "worker=%s test sandbox operation failed on server=%s: %s",
                        worker_id,
                        active_server.name,
                        logger.redact_error(exc),
                    )
                continue

            candidates = traffic_tables(config, inventory)
            if not candidates:
                logger.warning("worker=%s server=%s has no visible business tables", worker_id, active_server.name)
                if stop_event.wait(1.0):
                    break
                continue
            connection = ensure_connection(active_server)
            if connection is None:
                continue
            connection_lost = False
            target_queries = (
                random.randint(config.transactions.min_queries, config.transactions.max_queries)
                if config.transactions.enabled
                else 1
            )
            active_database = "-"
            active_table = "-"
            if config.transactions.enabled:
                if not transaction_control("START TRANSACTION", "transaction_begin"):
                    if connection is None:
                        continue
                    logger.error("worker=%s cannot start transaction on server=%s", worker_id, active_server.name)
                    break
                transaction_active = True
            completed_queries = 0
            try:
                for _ in range(target_queries):
                    if stop_event.is_set() or connection is None:
                        break
                    ref = weighted_table_ref(config, candidates)
                    active_database, active_table = ref.database, ref.table
                    cache_key = (active_server.name, ref.database, ref.table)
                    table = table_cache.get(cache_key)
                    if table is None:
                        table = load_table_info(connection, ref.table, ref.estimated_rows, ref.database)
                        if not table.columns:
                            logger.warning("server=%s table=%s.%s has no visible columns", active_server.name, ref.database, ref.table)
                            continue
                        table_cache[cache_key] = table
                    plan = make_query(
                        table,
                        config.query,
                        lambda sql, params, kind: execute_select(sql, params, kind),
                    )
                    if stop_event.is_set() or connection is None:
                        break
                    execute_select(plan.sql, plan.params, plan.query_type)
                    completed_queries += 1
                    if connection is None:
                        break
                if transaction_active:
                    complete = completed_queries == target_queries and not stop_event.is_set()
                    if complete and connection is not None:
                        if transaction_control("COMMIT", "transaction_commit"):
                            transaction_active = False
                        else:
                            rollback_transaction()
                    else:
                        rollback_transaction()
            except (pymysql.MySQLError, ValueError) as exc:
                rollback_transaction()
                logger.error(
                    "worker=%s query generation failed on server=%s: %s",
                    worker_id,
                    active_server.name,
                    logger.redact_error(exc),
                )
                if stop_event.wait(1.0):
                    break
            connection = None
    finally:
        for open_connection in connection_by_server.values():
            try:
                open_connection.close()
            except pymysql.MySQLError as exc:
                logger.warning("worker=%s connection close failed: %s", worker_id, logger.redact_error(exc))


def print_cluster_banner(config: AppConfig, inventories: dict[str, ServerInventory], test_enabled: bool) -> None:
    database_count = sum(len(inventory.databases) for inventory in inventories.values())
    table_count = sum(len(traffic_tables(config, inventory)) for inventory in inventories.values())
    print("=" * 48)
    print("       MySQL Multi-Server Traffic Generator")
    print("=" * 48)
    print(f"\nServers: {len(inventories)}")
    for inventory in inventories.values():
        print(f"  - {inventory.server.name}: {len(inventory.databases)} databases, {len(traffic_tables(config, inventory))} business tables")
    print(f"\nTotal: {database_count} databases, {table_count} enabled business tables")
    print(f"\nWorkers: {config.traffic.workers}")
    if config.traffic.mode == "interval":
        print(f"\nInterval: {config.traffic.interval_min:.1f} - {config.traffic.interval_max:.1f} seconds")
    else:
        print(f"\nTarget QPS: {config.traffic.qps:g}")
    print(f"\nTransactions: {config.transactions.min_queries}-{config.transactions.max_queries} SELECT rounds per server transaction" if config.transactions.enabled else "\nTransactions: disabled")
    if test_enabled:
        print("\nTest sandbox: enabled; CRUD is restricted to verified generated test databases")
    else:
        print("\nSafety: business databases use SELECT only; test sandbox CRUD is disabled")
    if config.logging.file_enabled:
        print(f"\nLogs: {config.logging.file_path}")
    if config.logging.jsonl_enabled:
        print(config.logging.jsonl_path)
    print("\nPress Ctrl+C to stop.\n" + "=" * 48, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate controlled MySQL traffic; business schemas remain SELECT-only.")
    parser.add_argument("--config", default="config.yaml", help="YAML configuration path (default: config.yaml)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="test every configured server with SELECT 1")
    mode.add_argument("--list-tables", action="store_true", help="list visible databases and base tables on every server")
    mode.add_argument("--inspect", action="store_true", help="show columns and row estimates for visible tables on every server")
    mode.add_argument("--init-test-db", action="store_true", help="create or verify each reserved test database and seed synthetic rows, then exit")
    parser.add_argument("--test-crud", action="store_true", help="enable random CRUD traffic in reserved test databases while business schemas remain SELECT-only")
    args = parser.parse_args(argv)

    if args.test_crud and (args.check or args.list_tables or args.inspect or args.init_test_db):
        parser.error("--test-crud is a traffic mode and cannot be combined with inspection or initialization modes")

    logger: TrafficLogger | None = None
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load_config(config_path)
        logger = TrafficLogger(config, config_path.parent)
        if args.check:
            _, failed = check_servers(config, logger)
            return 1 if failed else 0
        if args.init_test_db:
            _, failed = check_servers(config, logger)
            if failed:
                raise ConfigError("Test database initialization stopped because one or more servers are unreachable.")
            for server in config.servers:
                database = initialize_sandbox(config, server, logger)
                print(f"Test database ready: {server.name}/{database}")
            return 0

        inventories, failed_servers = discover_cluster(config, logger)
        if not inventories:
            raise ConfigError("No configured MySQL server could be reached; see errors above.")
        if args.list_tables:
            print_cluster_listing(inventories)
            return 1 if failed_servers else 0
        if args.inspect:
            print_cluster_inspection(config, inventories, logger)
            return 1 if failed_servers else 0

        test_databases: dict[str, str] = {}
        if args.test_crud:
            if failed_servers:
                raise ConfigError("Test CRUD startup stopped because one or more configured servers could not be discovered.")
            for server in config.servers:
                if server.name not in inventories:
                    raise ConfigError(f"Test sandbox setup stopped because server {server.name} is not reachable.")
                test_databases[server.name] = initialize_sandbox(config, server, logger)

        if config.tables.mode == "manual":
            present = {
                ref.table
                for inventory in inventories.values()
                for ref in traffic_tables(config, inventory)
            }
            missing = [name for name in config.tables.include if name not in present]
            if missing and not test_databases:
                raise ConfigError("Configured table(s) not found on accessible servers: " + ", ".join(missing))
        target_count = sum(len(traffic_tables(config, inventory)) for inventory in inventories.values())
        if target_count == 0 and not test_databases:
            raise ConfigError("No visible business base tables are available. Check database SELECT grants.")

        print_cluster_banner(config, inventories, bool(test_databases))

        stats = TrafficStats()
        stop_event = threading.Event()
        pacer = TrafficPacer(config)
        workers = [
            threading.Thread(
                target=cluster_worker_main,
                name=f"mysql-traffic-worker-{index}",
                args=(index, config, inventories, test_databases, logger, stats, pacer, stop_event),
                daemon=False,
            )
            for index in range(1, config.traffic.workers + 1)
        ]
        for worker in workers:
            worker.start()

        def report_stats() -> None:
            while not stop_event.wait(config.stats.interval_seconds):
                if config.stats.enabled:
                    stats.print_summary()

        reporter = threading.Thread(target=report_stats, name="mysql-traffic-stats", daemon=True)
        reporter.start()
        try:
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    worker.join(timeout=0.25)
        except KeyboardInterrupt:
            print("\nStopping traffic generation...", flush=True)
            stop_event.set()
        finally:
            stop_event.set()
            for worker in workers:
                worker.join()
            if config.stats.enabled:
                stats.print_summary(final=True)
        return 0
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"Configuration/startup error: {exc}", file=sys.stderr)
        return 2
    except pymysql.MySQLError as exc:
        message = logger.redact_error(exc) if logger else str(exc)
        print(f"Database error: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0
    finally:
        if logger is not None:
            logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
