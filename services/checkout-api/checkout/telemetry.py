"""One JSON line per request: the structured log, with the metrics embedded.

CloudWatch reads the `_aws` block as Embedded Metric Format and turns the
fields it names into high-resolution metrics; the rest of the line stays a
searchable log event. The line is printed, not logged: Lambda's JSON log
format passes a line that is already JSON through untouched, but the logging
module would nest it under "message", where CloudWatch can't see `_aws`.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

NAMESPACE = "checkout-api"

# Metric name to unit. A line declares only the metrics it carries.
SERVICE_METRICS = {
    "error": "Count",
    "latency_ms": "Milliseconds",
    "pool_wait_time_ms": "Milliseconds",
    "pool_active_connections": "Count",
    "concurrent_requests": "Count",
    "db_query_ms": "Milliseconds",
}
# Per upstream, with the line's `dependency` field as the dimension.
UPSTREAM_METRICS = {
    "upstream_latency_ms": "Milliseconds",
    "upstream_timeout": "Count",
}


def log_line(fields: Mapping[str, Any], at: float, metrics: bool = True) -> str:
    """The request's log line, timestamped `at` (epoch seconds). Fields set to None are left out."""
    record: dict[str, Any] = {"timestamp": _iso(at)}
    record.update((name, value) for name, value in fields.items() if value is not None)
    if metrics:
        directives = [
            directive
            for directive in (
                _directive(record, SERVICE_METRICS, []),
                _directive(record, UPSTREAM_METRICS, ["dependency"]),
            )
            if directive is not None
        ]
        if directives:
            record["_aws"] = {"Timestamp": round(at * 1000), "CloudWatchMetrics": directives}
    return json.dumps(record, separators=(",", ":"))


def _directive(
    record: Mapping[str, Any], metrics: Mapping[str, str], dimensions: list[str]
) -> dict[str, Any] | None:
    names = [name for name in metrics if name in record]
    if not names or any(dimension not in record for dimension in dimensions):
        return None
    return {
        "Namespace": NAMESPACE,
        "Dimensions": [dimensions],
        "Metrics": [{"Name": name, "Unit": metrics[name], "StorageResolution": 1} for name in names],
    }


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
