"""Regression coverage for slow startup metadata and normal query limits."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymysql

import traffic
from src.config import load_config
from src.database import connect


class DelayedMetadataCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.sql = sql
        if "information_schema.TABLES" in sql and self.connection.read_timeout < 12:
            raise pymysql.OperationalError(2013, "Lost connection during slow metadata query")

    def fetchone(self):
        return (1,)

    def fetchall(self):
        if "information_schema.SCHEMATA" in self.sql:
            return [("business",)]
        return [("business", "records", 100)]


class DelayedMetadataConnection:
    def __init__(self, **options):
        self.read_timeout = options["read_timeout"]
        self.closed = False

    def cursor(self):
        return DelayedMetadataCursor(self)

    def close(self):
        self.closed = True


class MetadataTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
        self.logger = SimpleNamespace(info=Mock(), error=Mock(), redact_error=str)

    def test_slow_metadata_does_not_exclude_reachable_server(self):
        with patch("src.database._SelectOnlyConnection", side_effect=DelayedMetadataConnection):
            inventories, failed = traffic.discover_cluster(self.config, self.logger)
        self.assertEqual(failed, 0)
        inventory = inventories[self.config.servers[0].name]
        self.assertEqual(inventory.databases, ["business"])
        self.assertEqual(len(inventory.tables), 1)

    def test_regular_connections_keep_the_original_read_timeout(self):
        with patch("src.database._SelectOnlyConnection", side_effect=DelayedMetadataConnection):
            connection = connect(self.config, self.config.servers[0])
        expected = max(5, self.config.query.max_execution_time_ms / 1000 + 5)
        self.assertEqual(connection.read_timeout, expected)


if __name__ == "__main__":
    unittest.main()
