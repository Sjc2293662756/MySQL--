"""Human-readable and JSON Lines traffic logging with rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.config import AppConfig


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.msg, ensure_ascii=False, default=str)


class TrafficLogger:
    """Own the application logger and write structured query events."""

    def __init__(self, config: AppConfig, base_dir: Path) -> None:
        self.passwords = tuple(dict.fromkeys(
            password for password in (config.mysql.password, *(server.password for server in config.servers)) if password
        ))
        self.logger = logging.getLogger(f"mysql_traffic_generator.{id(self)}")
        self.logger.setLevel(getattr(logging, config.logging.level))
        self.logger.propagate = False
        self.handlers: list[logging.Handler] = []
        self.json_handler: logging.Handler | None = None
        self.lock = threading.Lock()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

        if config.logging.console:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.handlers.append(handler)
        if config.logging.file.enabled:
            path = self._resolve_path(base_dir, config.logging.file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=config.logging.file.max_size_mb * 1024 * 1024,
                backupCount=config.logging.file.backups,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.handlers.append(handler)
        if not self.logger.handlers:
            handler = logging.NullHandler()
            self.logger.addHandler(handler)
            self.handlers.append(handler)
        if config.logging.jsonl.enabled:
            path = self._resolve_path(base_dir, config.logging.jsonl.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=config.logging.jsonl.max_size_mb * 1024 * 1024,
                backupCount=config.logging.jsonl.backups,
                encoding="utf-8",
            )
            handler.setFormatter(_JsonFormatter())
            self.json_handler = handler
            self.handlers.append(handler)

    @staticmethod
    def _resolve_path(base_dir: Path, configured_path: str) -> Path:
        path = Path(configured_path).expanduser()
        return path if path.is_absolute() else base_dir / path

    def redact_error(self, error: object) -> str:
        message = str(error)
        for password in self.passwords:
            message = message.replace(password, "[REDACTED]")
        return message

    def info(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.logger.info(self.redact_error(rendered))

    def warning(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.logger.warning(self.redact_error(rendered))

    def error(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.logger.error(self.redact_error(rendered))

    def query(
        self,
        *,
        worker: int,
        server: str | None = None,
        database: str,
        table: str,
        query_type: str,
        sql: str,
        params: tuple[object, ...],
        duration_ms: float,
        rows: int,
        success: bool,
        error: str | None,
    ) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        safe_params = _json_value(params)
        if self.passwords:
            safe_params = self._redact_json_strings(safe_params)
        event = {
            "timestamp": timestamp,
            "worker": worker,
            "server": server,
            "database": database,
            "table": table,
            "query_type": query_type,
            "sql": self.redact_error(sql),
            "params": safe_params,
            "duration_ms": round(duration_ms, 3),
            "rows": rows,
            "success": success,
        }
        if error:
            event["error"] = self.redact_error(error)
        with self.lock:
            self.logger.info(
                "worker=%s server=%s database=%s table=%s type=%s sql=%s params=%s duration=%.2fms rows=%s status=%s%s",
                worker,
                server or "-",
                database,
                table,
                query_type,
                event["sql"],
                json.dumps(safe_params, ensure_ascii=False, default=str),
                duration_ms,
                rows,
                "success" if success else "failed",
                f" error={event['error']}" if error else "",
            )
            if self.json_handler is not None:
                self.json_handler.emit(logging.makeLogRecord({"msg": event, "levelno": logging.INFO, "levelname": "INFO"}))
                self.json_handler.flush()

    def _redact_json_strings(self, value: Any) -> Any:
        if isinstance(value, str):
            for password in self.passwords:
                value = value.replace(password, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact_json_strings(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_json_strings(item) for key, item in value.items()}
        return value

    def close(self) -> None:
        for handler in self.handlers:
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)
