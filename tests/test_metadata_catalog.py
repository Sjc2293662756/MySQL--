"""Catalog enumeration avoids global statistics while preserving COUNT guards."""

import unittest

import pymysql

from src.database import list_database_tables, load_table_info


class MetadataCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.sql = sql
        self.connection.calls.append((sql, params))
        if "information_schema.TABLES" in sql and "TABLE_ROWS" in sql and "TABLE_NAME = %s" not in sql:
            raise pymysql.OperationalError(2013, "Global table statistics exceed the read budget")

    def fetchall(self):
        if "information_schema.COLUMNS" in self.sql:
            return [("id", "bigint", "PRI"), ("payload", "varchar(20)", "")]
        return [("business", "records", None)]

    def fetchone(self):
        return (25,)


class MetadataConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return MetadataCursor(self)


class MetadataCatalogTests(unittest.TestCase):
    def test_global_catalog_does_not_open_all_table_statistics(self):
        connection = MetadataConnection()
        self.assertEqual(list_database_tables(connection), [("business", "records", None)])

    def test_selected_table_loads_its_row_estimate_for_count_guard(self):
        connection = MetadataConnection()
        table = load_table_info(connection, "records", database="business")
        self.assertEqual(table.estimated_rows, 25)
        statistics = [call for call in connection.calls if "TABLE_ROWS" in call[0]]
        self.assertEqual(len(statistics), 1)
        self.assertEqual(statistics[0][1], ("business", "records"))

    def test_existing_estimate_does_not_trigger_another_statistics_query(self):
        connection = MetadataConnection()
        table = load_table_info(connection, "records", 12, "business")
        self.assertEqual(table.estimated_rows, 12)
        self.assertFalse(any("TABLE_ROWS" in sql for sql, _ in connection.calls))


if __name__ == "__main__":
    unittest.main()
