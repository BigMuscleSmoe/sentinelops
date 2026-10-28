"""checkout-api's service metrics as one table: the first thing to read.

Metrics are extracted from the same log lines as the logs, but they're queryable
within seconds, while Logs Insights and log search can trail by minutes under
load. So the agent reads these first and treats them as the primary source.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinelops_tools.env import ToolEnv, aws_errors_as_text, clock

MAX_ROWS = 90
MAX_MINUTES = 180
MAX_PAGES = 5
# How late a period's last metrics can land: CloudWatch makes a request's
# metrics queryable 2-11 s after it ends (measured 2026-10-21).
SETTLING_S = 20
# CloudWatch keeps 10-second data for 3 hours, then only 1-minute data. The
# margin keeps a window that's nearly 3 hours old from coming back half empty.
HIGH_RES_KEPT = timedelta(hours=3) - timedelta(minutes=5)


@dataclass(frozen=True)
class Column:
    id: str
    metric: str
    stat: str
    header: str
    dependency: str | None = None


# Every column the runbooks name. A column with no data shows "-".
COLUMNS = [
    Column("requests", "latency_ms", "SampleCount", "req"),
    Column("errors", "error", "Average", "err%"),
    Column("lat_p50", "latency_ms", "p50", "p50_ms"),
    Column("lat_p99", "latency_ms", "p99", "p99_ms"),
    Column("pool_wait", "pool_wait_time_ms", "p99", "pool_wait_p99_ms"),
    Column("pool_active", "pool_active_connections", "Maximum", "pool_active_max"),
    Column("concurrent", "concurrent_requests", "Maximum", "concurrent_max"),
    Column("db_query", "db_query_ms", "p50", "db_query_p50_ms"),
    Column("pay_p99", "upstream_latency_ms", "p99", "payments_p99_ms", "payments-api"),
    Column("pay_timeouts", "upstream_timeout", "Sum", "payments_timeouts", "payments-api"),
]


@aws_errors_as_text("the metrics")
def metrics_table(env: ToolEnv, minutes: int = 10, period_seconds: int = 10) -> str:
    if period_seconds not in (10, 60):
        return "period_seconds must be 10 or 60."
    notes = []
    end = env.now().astimezone(UTC)
    minutes = max(1, minutes)
    if period_seconds == 10 and end - timedelta(minutes=minutes) < env.wall_clock() - HIGH_RES_KEPT:
        notes.append("10-second data is kept for 3 hours; this window is older, so it's in 60 s periods.")
        period_seconds = 60
    allowed = min(MAX_MINUTES, MAX_ROWS * period_seconds // 60)
    if minutes > allowed:
        notes.append(f"Showing the last {allowed} minutes: at most {MAX_ROWS} rows per call.")
        minutes = allowed

    start = _floor(end - timedelta(minutes=minutes), period_seconds)
    # CloudWatch leaves out a period that runs past EndTime, so ending at `now`
    # would hide the current period, the one the alarm is about. End on the next
    # boundary instead. A replay can then see up to one period past its now.
    series = _query(env, start, _ceil(end, period_seconds), period_seconds)

    times = sorted({t for values in series.values() for t in values})
    if not times:
        return (
            f"No checkout-api metrics for {clock(start)} to {clock(end)} UTC: no requests, "
            "or their metrics haven't landed yet."
        )

    header = ["time"] + [c.header for c in COLUMNS]
    rows = [header]
    for t in times:
        unsettled = t + timedelta(seconds=period_seconds) > end - timedelta(seconds=SETTLING_S)
        rows.append(
            [clock(t) + ("*" if unsettled else "")]
            + [_cell(c, series[c.id].get(t)) for c in COLUMNS]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    table = "\n".join(
        "  ".join(
            cell.ljust(width) if i == 0 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))
        )
        for row in rows
    )

    lines = [
        f"checkout-api metrics, {period_seconds} s periods, {clock(start)} to {clock(end)} UTC, "
        "oldest first. Each row starts at its time. err% is the share of requests that failed; "
        "a timeout to payments-api counts under payments_timeouts, not err%.",
        *notes,
        table,
    ]
    if any(row[0].endswith("*") for row in rows[1:]):
        lines.append(f"* still filling in: metrics land up to {SETTLING_S} s after the requests.")
    return "\n".join(lines)


def _query(env: ToolEnv, start: datetime, end: datetime, period: int) -> dict[str, dict[datetime, float]]:
    queries = []
    for c in COLUMNS:
        dimensions = [{"Name": "dependency", "Value": c.dependency}] if c.dependency else []
        queries.append(
            {
                "Id": c.id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": env.namespace,
                        "MetricName": c.metric,
                        "Dimensions": dimensions,
                    },
                    "Period": period,
                    "Stat": c.stat,
                },
            }
        )
    series: dict[str, dict[datetime, float]] = {c.id: {} for c in COLUMNS}
    request: dict[str, Any] = {
        "MetricDataQueries": queries,
        "StartTime": start,
        "EndTime": end,
        "ScanBy": "TimestampAscending",
    }
    for _ in range(MAX_PAGES):
        response = env.cloudwatch.get_metric_data(**request)
        for result in response["MetricDataResults"]:
            for t, value in zip(result["Timestamps"], result["Values"]):
                series[result["Id"]][t.astimezone(UTC)] = value
        token = response.get("NextToken")
        if not token:
            break
        request["NextToken"] = token
    return series


def _floor(moment: datetime, seconds: int) -> datetime:
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


def _ceil(moment: datetime, seconds: int) -> datetime:
    floor = _floor(moment, seconds)
    return floor if floor == moment else floor + timedelta(seconds=seconds)


def _cell(column: Column, value: float | None) -> str:
    if value is None:
        return "-"
    if column.id == "errors":
        return f"{value * 100:.1f}"
    if column.stat in ("SampleCount", "Sum", "Maximum"):
        return f"{value:.0f}"
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}"
