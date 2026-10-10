"""YAML configuration loading and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the YAML configuration is invalid."""


@dataclass(frozen=True)
class ReconnectConfig:
    enabled: bool = True
    interval_seconds: float = 5.0
    max_retries: int = 0


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    connect_timeout: int = 5
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    ssl_disabled: bool = False


@dataclass(frozen=True)
class ServerConfig:
    name: str
    host: str
    port: int
    user: str
    password: str
    charset: str = "utf8mb4"
    connect_timeout: int = 5
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    ssl_disabled: bool = False


@dataclass(frozen=True)
class TestSandboxConfig:
    database_prefix: str = "codex_traffic_test_"
    seed_rows: int = 20
    traffic_probability: float = 0.25
    operation_weights: dict[str, float] = field(default_factory=lambda: {
        "select": 25,
        "insert": 25,
        "update": 25,
        "delete": 25,
    })


@dataclass(frozen=True)
class TablesConfig:
    mode: str = "auto"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class BurstConfig:
    enabled: bool = False
    probability: float = 0.05
    multiplier: float = 3.0
    duration_seconds: float = 10.0


@dataclass(frozen=True)
class TrafficConfig:
    workers: int = 3
    mode: str = "interval"
    interval_min: float = 1.0
    interval_max: float = 3.0
    qps: float = 5.0
    random_burst: BurstConfig = field(default_factory=BurstConfig)


@dataclass(frozen=True)
class QueryConfig:
    limit_values: tuple[int, ...] = (1, 5, 10, 20, 50)
    weights: dict[str, float] = field(default_factory=lambda: {
        "simple_select": 40,
        "primary_key": 20,
        "count": 10,
        "order_by": 15,
        "where": 15,
    })
    enable_count: bool = True
    count_max_estimated_rows: int = 100_000
    max_execution_time_ms: int = 3000


@dataclass(frozen=True)
class TransactionsConfig:
    enabled: bool = True
    min_queries: int = 2
    max_queries: int = 3


@dataclass(frozen=True)
class FileLogConfig:
    enabled: bool = True
    path: str = "logs/traffic.log"
    max_size_mb: int = 100
    backups: int = 5


@dataclass(frozen=True)
class JsonlConfig:
    enabled: bool = True
    path: str = "logs/traffic.jsonl"
    max_size_mb: int = 100
    backups: int = 5


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    console: bool = True
    file: FileLogConfig = field(default_factory=FileLogConfig)
    jsonl: JsonlConfig = field(default_factory=JsonlConfig)

    @property
    def file_enabled(self) -> bool:
        return self.file.enabled

    @property
    def file_path(self) -> str:
        return self.file.path

    @property
    def jsonl_enabled(self) -> bool:
        return self.jsonl.enabled

    @property
    def jsonl_path(self) -> str:
        return self.jsonl.path


@dataclass(frozen=True)
class StatsConfig:
    enabled: bool = True
    interval_seconds: float = 10.0


@dataclass(frozen=True)
class AppConfig:
    mysql: MysqlConfig
    servers: tuple[ServerConfig, ...]
    test_sandbox: TestSandboxConfig
    tables: TablesConfig
    traffic: TrafficConfig
    query: QueryConfig
    transactions: TransactionsConfig
    table_weights: dict[str, float]
    logging: LoggingConfig
    stats: StatsConfig


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a YAML mapping")
    return value


def _positive_number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        comparison = "non-negative" if allow_zero else "greater than zero"
        raise ConfigError(f"{name} must be {comparison}")
    return result


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        comparison = "non-negative" if allow_zero else "greater than zero"
        raise ConfigError(f"{name} must be {comparison}")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _reconnect_config(raw: Any, name: str) -> ReconnectConfig:
    reconnect_raw = _mapping(raw, name)
    return ReconnectConfig(
        enabled=_boolean(reconnect_raw.get("enabled", True), f"{name}.enabled"),
        interval_seconds=_positive_number(reconnect_raw.get("interval_seconds", 5), f"{name}.interval_seconds"),
        max_retries=_positive_int(reconnect_raw.get("max_retries", 0), f"{name}.max_retries", allow_zero=True),
    )


def _parse_servers(root: dict[str, Any]) -> tuple[MysqlConfig, tuple[ServerConfig, ...]]:
    servers_raw = root.get("servers")
    if servers_raw is not None:
        if not isinstance(servers_raw, list) or not servers_raw:
            raise ConfigError("servers must be a non-empty list")
        servers: list[ServerConfig] = []
        names: set[str] = set()
        for index, item in enumerate(servers_raw):
            name = f"servers[{index}]"
            raw = _mapping(item, name)
            server_name = raw.get("name")
            if not isinstance(server_name, str) or not server_name.strip():
                raise ConfigError(f"{name}.name must be a non-empty string")
            server_name = server_name.strip()
            if server_name in names:
                raise ConfigError(f"server name is duplicated: {server_name}")
            names.add(server_name)
            fields: dict[str, str] = {}
            for key in ("host", "user"):
                value = raw.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ConfigError(f"{name}.{key} must be a non-empty string")
                fields[key] = value.strip()
            password = raw.get("password", "")
            if not isinstance(password, str):
                raise ConfigError(f"{name}.password must be a string")
            charset = raw.get("charset", "utf8mb4")
            if not isinstance(charset, str) or not charset.strip():
                raise ConfigError(f"{name}.charset must be a non-empty string")
            port = _positive_int(raw.get("port", 3306), f"{name}.port")
            if port > 65535:
                raise ConfigError(f"{name}.port must not exceed 65535")
            servers.append(ServerConfig(
                name=server_name,
                host=fields["host"],
                port=port,
                user=fields["user"],
                password=password,
                charset=charset.strip(),
                connect_timeout=_positive_int(raw.get("connect_timeout", 5), f"{name}.connect_timeout"),
                reconnect=_reconnect_config(raw.get("reconnect", {}), f"{name}.reconnect"),
                ssl_disabled=_boolean(raw.get("ssl_disabled", False), f"{name}.ssl_disabled"),
            ))
        first = servers[0]
        legacy = MysqlConfig(first.host, first.port, first.user, first.password, "", first.charset, first.connect_timeout, first.reconnect, first.ssl_disabled)
        return legacy, tuple(servers)

    mysql_raw = _mapping(root.get("mysql"), "mysql")
    reconnect = _reconnect_config(mysql_raw.get("reconnect", {}), "mysql.reconnect")
    required_strings = {}
    for key in ("host", "user", "database"):
        value = mysql_raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"mysql.{key} must be a non-empty string")
        required_strings[key] = value.strip()
    password = mysql_raw.get("password", "")
    if not isinstance(password, str):
        raise ConfigError("mysql.password must be a string")
    charset = mysql_raw.get("charset", "utf8mb4")
    if not isinstance(charset, str) or not charset.strip():
        raise ConfigError("mysql.charset must be a non-empty string")
    port = _positive_int(mysql_raw.get("port", 3306), "mysql.port")
    if port > 65535:
        raise ConfigError("mysql.port must not exceed 65535")
    connect_timeout = _positive_int(mysql_raw.get("connect_timeout", 5), "mysql.connect_timeout")
    ssl_disabled = _boolean(mysql_raw.get("ssl_disabled", False), "mysql.ssl_disabled")
    mysql = MysqlConfig(required_strings["host"], port, required_strings["user"], password,
                        required_strings["database"], charset.strip(), connect_timeout, reconnect, ssl_disabled)
    server = ServerConfig(required_strings["host"], required_strings["host"], port,
                          required_strings["user"], password, charset.strip(), connect_timeout, reconnect, ssl_disabled)
    return mysql, (server,)


def load_config(path: str | Path) -> AppConfig:
    """Read a YAML file, apply documented defaults, and validate all settings."""
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    root = _mapping(raw, "configuration")

    mysql, servers = _parse_servers(root)

    tables_raw = _mapping(root.get("tables", {}), "tables")
    table_mode = tables_raw.get("mode", "auto")
    if table_mode not in {"auto", "manual"}:
        raise ConfigError("tables.mode must be 'auto' or 'manual'")
    include = _string_list(tables_raw.get("include", []), "tables.include")
    exclude = _string_list(tables_raw.get("exclude", []), "tables.exclude")
    if table_mode == "manual" and not include:
        raise ConfigError("tables.include must contain at least one table in manual mode")
    tables = TablesConfig(table_mode, include, exclude)

    traffic_raw = _mapping(root.get("traffic", {}), "traffic")
    interval_raw = _mapping(traffic_raw.get("interval", {}), "traffic.interval")
    burst_raw = _mapping(traffic_raw.get("random_burst", {}), "traffic.random_burst")
    traffic_mode = traffic_raw.get("mode", "interval")
    if traffic_mode not in {"interval", "qps"}:
        raise ConfigError("traffic.mode must be 'interval' or 'qps'")
    interval_min = _positive_number(interval_raw.get("min", 1), "traffic.interval.min")
    interval_max = _positive_number(interval_raw.get("max", 3), "traffic.interval.max")
    if interval_min > interval_max:
        raise ConfigError("traffic.interval.min must be less than or equal to traffic.interval.max")
    burst_probability = _positive_number(burst_raw.get("probability", 0.05), "traffic.random_burst.probability", allow_zero=True)
    if burst_probability > 1:
        raise ConfigError("traffic.random_burst.probability must be between 0 and 1")
    burst = BurstConfig(
        enabled=_boolean(burst_raw.get("enabled", False), "traffic.random_burst.enabled"),
        probability=burst_probability,
        multiplier=_positive_number(burst_raw.get("multiplier", 3), "traffic.random_burst.multiplier"),
        duration_seconds=_positive_number(burst_raw.get("duration_seconds", 10), "traffic.random_burst.duration_seconds"),
    )
    if burst.multiplier < 1:
        raise ConfigError("traffic.random_burst.multiplier must be at least 1")
    traffic = TrafficConfig(
        workers=_positive_int(traffic_raw.get("workers", 3), "traffic.workers"),
        mode=traffic_mode,
        interval_min=interval_min,
        interval_max=interval_max,
        qps=_positive_number(traffic_raw.get("qps", 5), "traffic.qps"),
        random_burst=burst,
    )

    query_raw = _mapping(root.get("query", {}), "query")
    limits_value = query_raw.get("limit_values", [1, 5, 10, 20, 50])
    if not isinstance(limits_value, list) or not limits_value:
        raise ConfigError("query.limit_values must be a non-empty list of positive integers")
    limits = tuple(_positive_int(item, "query.limit_values[]") for item in limits_value)
    weights_raw = _mapping(query_raw.get("weights", {
        "simple_select": 40, "primary_key": 20, "count": 10, "order_by": 15, "where": 15
    }), "query.weights")
    allowed_weights = {"simple_select", "primary_key", "count", "order_by", "where"}
    unknown_weights = set(weights_raw) - allowed_weights
    if unknown_weights:
        raise ConfigError("Unknown query weight(s): " + ", ".join(sorted(unknown_weights)))
    weights = {key: _positive_number(value, f"query.weights.{key}", allow_zero=True) for key, value in weights_raw.items()}
    if not any(value > 0 for value in weights.values()):
        raise ConfigError("At least one query.weights value must be greater than zero")
    query = QueryConfig(
        limit_values=limits,
        weights=weights,
        enable_count=_boolean(query_raw.get("enable_count", True), "query.enable_count"),
        count_max_estimated_rows=_positive_int(query_raw.get("count_max_estimated_rows", 100_000), "query.count_max_estimated_rows", allow_zero=True),
        max_execution_time_ms=_positive_int(query_raw.get("max_execution_time_ms", 3000), "query.max_execution_time_ms", allow_zero=True),
    )

    transactions_raw = _mapping(root.get("transactions", {}), "transactions")
    transactions = TransactionsConfig(
        enabled=_boolean(transactions_raw.get("enabled", True), "transactions.enabled"),
        min_queries=_positive_int(transactions_raw.get("min_queries", 2), "transactions.min_queries"),
        max_queries=_positive_int(transactions_raw.get("max_queries", 3), "transactions.max_queries"),
    )
    if transactions.min_queries > transactions.max_queries:
        raise ConfigError("transactions.min_queries must be less than or equal to transactions.max_queries")
    if transactions.max_queries > 20:
        raise ConfigError("transactions.max_queries must not exceed 20 to keep read-only transactions short")

    weights_raw_table = _mapping(root.get("table_weights", {}), "table_weights")
    table_weights = {str(name): _positive_number(value, f"table_weights.{name}", allow_zero=True) for name, value in weights_raw_table.items()}

    logging_raw = _mapping(root.get("logging", {}), "logging")
    file_raw = _mapping(logging_raw.get("file", {}), "logging.file")
    jsonl_raw = _mapping(logging_raw.get("jsonl", {}), "logging.jsonl")
    level = str(logging_raw.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    for name, value in (("logging.file.path", file_raw.get("path", "logs/traffic.log")), ("logging.jsonl.path", jsonl_raw.get("path", "logs/traffic.jsonl"))):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{name} must be a non-empty string")
    logging_config = LoggingConfig(
        level=level,
        console=_boolean(logging_raw.get("console", True), "logging.console"),
        file=FileLogConfig(
            enabled=_boolean(file_raw.get("enabled", True), "logging.file.enabled"),
            path=str(file_raw.get("path", "logs/traffic.log")),
            max_size_mb=_positive_int(file_raw.get("max_size_mb", 100), "logging.file.max_size_mb"),
            backups=_positive_int(file_raw.get("backups", 5), "logging.file.backups"),
        ),
        jsonl=JsonlConfig(
            enabled=_boolean(jsonl_raw.get("enabled", True), "logging.jsonl.enabled"),
            path=str(jsonl_raw.get("path", "logs/traffic.jsonl")),
            max_size_mb=_positive_int(jsonl_raw.get("max_size_mb", 100), "logging.jsonl.max_size_mb"),
            backups=_positive_int(jsonl_raw.get("backups", 5), "logging.jsonl.backups"),
        ),
    )
    stats_raw = _mapping(root.get("stats", {}), "stats")
    stats = StatsConfig(
        enabled=_boolean(stats_raw.get("enabled", True), "stats.enabled"),
        interval_seconds=_positive_number(stats_raw.get("interval_seconds", 10), "stats.interval_seconds"),
    )
    sandbox_raw = _mapping(root.get("test_sandbox", {}), "test_sandbox")
    database_prefix = sandbox_raw.get("database_prefix", "codex_traffic_test_")
    if (
        not isinstance(database_prefix, str)
        or not database_prefix
        or len(database_prefix) > 32
        or not database_prefix[0].isalpha()
        or any(not (char.isalnum() or char == "_") for char in database_prefix)
    ):
        raise ConfigError("test_sandbox.database_prefix must be 1-32 letters, digits, or underscores and start with a letter")
    seed_rows = _positive_int(sandbox_raw.get("seed_rows", 20), "test_sandbox.seed_rows")
    if seed_rows > 1000:
        raise ConfigError("test_sandbox.seed_rows must not exceed 1000")
    traffic_probability = _positive_number(
        sandbox_raw.get("traffic_probability", 0.25), "test_sandbox.traffic_probability", allow_zero=True
    )
    if traffic_probability > 1:
        raise ConfigError("test_sandbox.traffic_probability must be between 0 and 1")
    operation_weights_raw = _mapping(sandbox_raw.get("operation_weights", {
        "select": 25, "insert": 25, "update": 25, "delete": 25
    }), "test_sandbox.operation_weights")
    allowed_operations = {"select", "insert", "update", "delete"}
    unknown_operations = set(operation_weights_raw) - allowed_operations
    if unknown_operations:
        raise ConfigError("Unknown test_sandbox.operation_weights key(s): " + ", ".join(sorted(unknown_operations)))
    operation_weights = {
        key: _positive_number(value, f"test_sandbox.operation_weights.{key}", allow_zero=True)
        for key, value in operation_weights_raw.items()
    }
    if not any(value > 0 for value in operation_weights.values()):
        raise ConfigError("At least one test sandbox operation weight must be greater than zero")
    test_sandbox = TestSandboxConfig(database_prefix, seed_rows, traffic_probability, operation_weights)

    return AppConfig(
        mysql=mysql,
        servers=servers,
        test_sandbox=test_sandbox,
        tables=tables,
        traffic=traffic,
        query=query,
        transactions=transactions,
        table_weights=table_weights,
        logging=logging_config,
        stats=stats,
    )
