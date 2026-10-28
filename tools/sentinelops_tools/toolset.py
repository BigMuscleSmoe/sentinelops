"""The tools as the model sees them: names, parameters, and descriptions.

Each docstring here is sent to the model as the tool's description, so it's
written to the model. Implementation notes go in the modules behind them.
"""

from collections.abc import Callable
from typing import Literal

from sentinelops_tools import deploys, logs, metrics
from sentinelops_tools.env import ToolEnv

Route = Literal["GET /products/{id}", "POST /cart/items", "POST /checkout"]


def make_tools(env: ToolEnv) -> list[Callable[..., str]]:
    def get_metrics(minutes: int = 10, period_seconds: Literal[10, 60] = 10) -> str:
        """checkout-api's service metrics as a table, one row per period, oldest first.

        Read this first. Metrics are complete within seconds, while logs can lag by
        minutes under load. Columns: requests, error rate, latency p50 and p99, pool
        wait p99, pool connections in use, concurrent requests, database query time,
        and payments-api latency and timeouts.

        Args:
            minutes: How far back to look, ending now. At most 90 rows come back.
            period_seconds: 10, the alarms' period, or 60 for a longer view.
        """
        return metrics.metrics_table(env, minutes=minutes, period_seconds=period_seconds)

    def get_recent_logs(
        minutes: int = 10, errors_only: bool = True, route: Route | None = None, limit: int = 20
    ) -> str:
        """checkout-api's newest request log lines, whole, oldest first.

        Each line is one request: route, status, latency, version, pool fields, and
        for a failure its error type, message, and stack. Logs can trail the metrics
        by minutes under load, so a missing recent line proves nothing alone.

        Args:
            minutes: How far back to search, ending now.
            errors_only: Only failed requests (the default), or every request.
            route: Only this route.
            limit: How many of the newest matching lines to return, at most 200.
        """
        return logs.recent_lines(
            env, minutes=minutes, errors_only=errors_only, route=route, limit=limit
        )

    def get_deploy_history(limit: int = 10) -> str:
        """checkout-api's deploys and config changes, newest first, up to now.

        Each record has its time, kind (deploy or config), version, commit, author,
        message, and for a config change the keys with their old and new values.

        Args:
            limit: How many records, at most 20.
        """
        return deploys.history(env, limit=limit)

    return [get_metrics, get_recent_logs, get_deploy_history]
