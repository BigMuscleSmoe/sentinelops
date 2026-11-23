"""What a request's calls behind checkout-api take: the pool, the query, and payments-api.

A request asks the backend once, when it needs a connection, and gets back its
timeline: how long it queues for a connection from the shared pool, how long
its query holds it, and how long payments-api takes to answer the charge.
"""

from dataclasses import dataclass
from typing import Protocol

from checkout.config import Config


@dataclass(frozen=True)
class Timeline:
    started_at: float
    wait_s: float  # queueing for a connection; the acquire timeout if timed_out
    timed_out: bool
    hold_s: float  # the query, holding the connection
    upstream_s: float | None  # how long payments-api takes to answer; checkouts only
    active_connections: int  # connections in use once this request has its own
    concurrent_requests: int  # requests in flight on every instance, this one included

    @property
    def granted_at(self) -> float:
        return self.started_at + self.wait_s

    @property
    def released_at(self) -> float:
        return self.granted_at + self.hold_s


class Backend(Protocol):
    def connect(self, route: str, settings: Config) -> Timeline:
        """Take the request's place in the pool's queue, and return its timeline."""
        ...
