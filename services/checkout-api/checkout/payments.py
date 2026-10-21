"""Client for payments-api, the upstream that charges the card."""

from collections.abc import Callable

from sim.backend import Timeline

DEPENDENCY = "payments-api"


class UpstreamTimeout(Exception):
    """payments-api didn't answer within the client timeout; checkout-api answers 504."""


def charge(timeline: Timeline, timeout_s: float, sleep_until: Callable[[float], None]) -> None:
    """Charge the order. Runs after the order's connection is back in the pool."""
    response_s = timeline.upstream_s or 0.0
    if response_s > timeout_s:
        sleep_until(timeline.released_at + timeout_s)
        raise UpstreamTimeout(f"{DEPENDENCY} did not respond within {timeout_s * 1000:.0f} ms")
    sleep_until(timeline.released_at + response_s)
