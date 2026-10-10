"""Thread-safe runtime traffic counters and summaries."""

from __future__ import annotations

import threading
import time
from collections import Counter


class TrafficStats:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.total = 0
        self.success = 0
        self.failed = 0
        self.transactions_started = 0
        self.transactions_committed = 0
        self.transactions_rolled_back = 0
        self.transactions_aborted = 0
        self.latency_total = 0.0
        self.latency_min: float | None = None
        self.latency_max = 0.0
        self.tables: Counter[str] = Counter()
        self.query_types: Counter[str] = Counter()
        self.last_report_at = self.started
        self.last_report_total = 0
        self.last_report_success = 0
        self.last_report_failed = 0

    def record(self, table: str, query_type: str, duration_ms: float, rows: int, success: bool) -> None:
        del rows  # Row counts are recorded per event; aggregate traffic stats count queries.
        with self.lock:
            self.total += 1
            self.success += int(success)
            self.failed += int(not success)
            self.latency_total += duration_ms
            self.latency_min = duration_ms if self.latency_min is None else min(self.latency_min, duration_ms)
            self.latency_max = max(self.latency_max, duration_ms)
            self.tables[table] += 1
            self.query_types[query_type] += 1

    def record_transaction(self, outcome: str) -> None:
        counters = {
            "started": "transactions_started",
            "committed": "transactions_committed",
            "rolled_back": "transactions_rolled_back",
            "aborted": "transactions_aborted",
        }
        attribute = counters.get(outcome)
        if attribute is None:
            raise ValueError(f"Unknown transaction outcome: {outcome}")
        with self.lock:
            setattr(self, attribute, getattr(self, attribute) + 1)

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def print_summary(self, final: bool = False) -> None:
        now = time.monotonic()
        with self.lock:
            runtime = now - self.started
            total, success, failed = self.total, self.success, self.failed
            transactions = (
                self.transactions_started,
                self.transactions_committed,
                self.transactions_rolled_back,
                self.transactions_aborted,
            )
            current_qps = (total - self.last_report_total) / max(now - self.last_report_at, 0.001)
            average_qps = total / max(runtime, 0.001)
            average_latency = self.latency_total / total if total else 0.0
            min_latency = self.latency_min or 0.0
            max_latency = self.latency_max
            tables = self.tables.most_common(10)
            types = self.query_types.most_common()
            self.last_report_at = now
            self.last_report_total = total
            self.last_report_success = success
            self.last_report_failed = failed
        title = "Final MySQL Traffic Statistics" if final else "MySQL Traffic Statistics"
        print("\n" + "=" * 28 + f"\n{title}\n" + "=" * 28)
        print(f"Runtime: {self._duration(runtime)}")
        print(f"Queries: total={total} success={success} failed={failed}")
        print(
            "Read-only transactions: "
            f"started={transactions[0]} committed={transactions[1]} "
            f"rolled_back={transactions[2]} aborted={transactions[3]}"
        )
        print(f"QPS: current={current_qps:.2f} average={average_qps:.2f}")
        print(f"Latency (ms): avg={average_latency:.2f} min={min_latency:.2f} max={max_latency:.2f}")
        print("Top Tables:")
        for table, count in tables:
            print(f"  {table:<24} {count}")
        print("Query Types:")
        for query_type, count in types:
            print(f"  {query_type:<24} {count}")
        print("=" * 28, flush=True)
