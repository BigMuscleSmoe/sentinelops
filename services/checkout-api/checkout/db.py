"""Database access through the connection pool every instance shares.

The database is simulated (see sim/): the shared ledger decides how long a
request queues for a connection, and the workload model how long its query
holds one. This module plays the request's part.
"""

from collections.abc import Callable

from sim.backend import Timeline


def wait_for_connection(
    timeline: Timeline, pool_max_size: int, sleep_until: Callable[[float], None]
) -> None:
    """Block until the pool hands over a connection, or give up at the acquire timeout."""
    sleep_until(timeline.granted_at)
    if timeline.timed_out:
        raise TimeoutError(
            f"acquiring connection from pool (waited {timeline.wait_s * 1000:.0f} ms, "
            f"pool_max_size={pool_max_size})"
        )


def run_query(timeline: Timeline, sleep_until: Callable[[float], None]) -> None:
    """Hold the connection for the query, then return it to the pool."""
    sleep_until(timeline.released_at)
