"""Safe SELECT query generation based on discovered table metadata."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from src.config import QueryConfig
from src.database import TableInfo, quote_identifier


@dataclass(frozen=True)
class QueryPlan:
    sql: str
    params: tuple[object, ...]
    query_type: str


SampleExecutor = Callable[[str, tuple[object, ...], str], Sequence[tuple[object, ...]]]

_SIMPLE_TYPES = ("int", "bigint", "varchar", "char", "date", "datetime", "timestamp", "decimal", "float", "double", "smallint", "mediumint", "tinyint", "year")
_COMMON_ORDER_NAMES = {"id", "created_at", "updated_at", "create_time", "update_time"}


def _type_base(column_type: str) -> str:
    return re.split(r"[ (]", column_type.lower(), maxsplit=1)[0]


def _selected_columns(table: TableInfo) -> list[str]:
    eligible = [column.name for column in table.columns if _type_base(column.column_type) in _SIMPLE_TYPES]
    if not eligible:
        return [column.name for column in table.columns[:5]]
    count = random.randint(1, min(5, len(eligible)))
    return random.sample(eligible, count)


def _table_identifier(table: TableInfo) -> str:
    name = quote_identifier(table.name)
    return f"{quote_identifier(table.database)}.{name}" if table.database else name


def _select_sql(table: TableInfo, columns: Sequence[str]) -> str:
    projection = ", ".join(quote_identifier(name) for name in columns)
    return f"SELECT {projection} FROM {_table_identifier(table)}"


def _simple_select(table: TableInfo, config: QueryConfig) -> QueryPlan:
    columns = _selected_columns(table)
    sql = _select_sql(table, columns) + f" LIMIT {random.choice(config.limit_values)}"
    return QueryPlan(sql, (), "simple_select")


def _primary_key(table: TableInfo, config: QueryConfig, execute_sample: SampleExecutor) -> QueryPlan | None:
    if not table.primary_keys:
        return None
    primary = table.primary_keys
    projection = ", ".join(quote_identifier(name) for name in primary)
    ordering = ", ".join(f"{quote_identifier(name)} DESC" for name in primary)
    sample_sql = f"SELECT {projection} FROM {_table_identifier(table)} ORDER BY {ordering} LIMIT 100"
    samples = execute_sample(sample_sql, (), "primary_key_sample")
    samples = [row for row in samples if len(row) == len(primary) and all(value is not None for value in row)]
    if not samples:
        return None
    values = random.choice(samples)
    predicates = " AND ".join(f"{quote_identifier(name)} = %s" for name in primary)
    sql = f"SELECT * FROM {_table_identifier(table)} WHERE {predicates} LIMIT 1"
    return QueryPlan(sql, tuple(values), "primary_key")


def _where(table: TableInfo, config: QueryConfig, execute_sample: SampleExecutor) -> QueryPlan | None:
    candidates = [
        column
        for column in table.columns
        if _type_base(column.column_type) in _SIMPLE_TYPES
        and (column.is_indexed or (table.estimated_rows is not None and table.estimated_rows <= config.count_max_estimated_rows))
    ]
    if not candidates:
        return None
    column = random.choice(candidates)
    quoted_column = quote_identifier(column.name)
    sample_sql = f"SELECT {quoted_column} FROM {_table_identifier(table)} WHERE {quoted_column} IS NOT NULL LIMIT 20"
    samples = execute_sample(sample_sql, (), "where_sample")
    values = [row[0] for row in samples if row and row[0] is not None]
    if not values:
        return None
    sql = f"SELECT * FROM {_table_identifier(table)} WHERE {quoted_column} = %s LIMIT {random.choice(config.limit_values)}"
    return QueryPlan(sql, (random.choice(values),), "where")


def _order_by(table: TableInfo, config: QueryConfig) -> QueryPlan | None:
    candidates = [
        column.name
        for column in table.columns
        if column.name.casefold() in _COMMON_ORDER_NAMES and column.is_indexed
    ]
    if not candidates:
        return None
    column = quote_identifier(random.choice(candidates))
    direction = random.choice(("ASC", "DESC"))
    sql = f"{_select_sql(table, _selected_columns(table))} ORDER BY {column} {direction} LIMIT {random.choice(config.limit_values)}"
    return QueryPlan(sql, (), "order_by")


def make_query(table: TableInfo, config: QueryConfig, execute_sample: SampleExecutor) -> QueryPlan:
    """Choose a weighted strategy and create parameterized, bounded SELECT SQL."""
    available = ["simple_select"]
    if table.primary_keys:
        available.append("primary_key")
    if config.enable_count and table.estimated_rows is not None and table.estimated_rows <= config.count_max_estimated_rows:
        available.append("count")
    if any(column.name.casefold() in _COMMON_ORDER_NAMES and column.is_indexed for column in table.columns):
        available.append("order_by")
    if any(
        _type_base(column.column_type) in _SIMPLE_TYPES
        and (column.is_indexed or (table.estimated_rows is not None and table.estimated_rows <= config.count_max_estimated_rows))
        for column in table.columns
    ):
        available.append("where")

    choices = available
    weights = [config.weights.get(kind, 0.0) for kind in choices]
    if not any(weight > 0 for weight in weights):
        choices, weights = ["simple_select"], [1.0]
    query_type = random.choices(choices, weights=weights, k=1)[0]

    if query_type == "primary_key":
        plan = _primary_key(table, config, execute_sample)
        if plan is not None:
            return plan
    elif query_type == "count":
        return QueryPlan(f"SELECT COUNT(*) FROM {_table_identifier(table)}", (), "count")
    elif query_type == "order_by":
        plan = _order_by(table, config)
        if plan is not None:
            return plan
    elif query_type == "where":
        plan = _where(table, config, execute_sample)
        if plan is not None:
            return plan
    return _simple_select(table, config)
