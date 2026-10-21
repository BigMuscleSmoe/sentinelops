"""Everything checkout-api depends on, simulated: its database and payments-api.

A request asks once, when it needs a connection, and gets its whole timeline
back: how long it queues for a connection, how long its query holds it, and
how long payments-api takes to answer. Deciding all of it up front lets the
request take its place in the pool's ledger in a single write.
"""

import random
from dataclasses import dataclass

from checkout.config import Config
from sim.pool import Ledger
from sim.workload import sample_hold, sample_payments_latency

CHECKOUT = "POST /checkout"


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


class Backend:
    def __init__(self, ledger: Ledger, rng: random.Random) -> None:
        self._ledger = ledger
        self._rng = rng

    def connect(self, route: str, settings: Config) -> Timeline:
        hold_s = sample_hold(route, self._rng)
        upstream_s = sample_payments_latency(self._rng) if route == CHECKOUT else None
        # The request stays in flight after its connection is back, while payments-api answers.
        after_release_s = 0.0 if upstream_s is None else min(upstream_s, settings.payments_timeout_s)
        reservation = self._ledger.reserve(
            settings.pool_max_size, settings.pool_acquire_timeout_s, hold_s, after_release_s
        )
        return Timeline(
            started_at=reservation.planned_at,
            wait_s=reservation.wait_s,
            timed_out=reservation.timed_out,
            hold_s=hold_s,
            upstream_s=upstream_s,
            active_connections=reservation.active_connections,
            concurrent_requests=reservation.concurrent_requests,
        )
