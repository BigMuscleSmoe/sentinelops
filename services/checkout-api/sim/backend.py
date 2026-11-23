"""Everything checkout-api depends on, simulated: its database and payments-api.

A request asks once, when it needs a connection, and gets its whole timeline
back: how long it queues for a connection, how long its query holds it, and
how long payments-api takes to answer. Deciding all of it up front lets the
request take its place in the pool's ledger in a single write.

payments-api's mode comes from outside checkout-api's settings: a slow upstream
changes nothing this service is configured with.

Backend implements checkout/backend.py's protocol; checkout/ never imports sim/,
so nothing the read tool shows names the simulation.
"""

import random
from collections.abc import Callable

from checkout.backend import Timeline
from checkout.config import Config
from sim.pool import Ledger
from sim.workload import HEALTHY, sample_hold, sample_payments_latency

CHECKOUT = "POST /checkout"


def _healthy() -> str:
    return HEALTHY


class Backend:  # checkout.backend.Backend
    def __init__(self, ledger: Ledger, rng: random.Random, payments: Callable[[], str] = _healthy) -> None:
        self._ledger = ledger
        self._rng = rng
        self._payments = payments  # payments-api's mode for the request being served

    def connect(self, route: str, settings: Config) -> Timeline:
        hold_s = sample_hold(route, self._rng)
        upstream_s = sample_payments_latency(self._rng, self._payments()) if route == CHECKOUT else None
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
