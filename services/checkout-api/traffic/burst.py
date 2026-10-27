"""The burst: the traffic every break sends checkout-api, so a fault shows
within a minute.

The fault controller starts it on every break, whatever the mode, so the
traffic's shape gives nothing away. Requests arrive as a Poisson process whose
rate ramps from START_RATE to PEAK_RATE over RAMP_S, then holds at the peak
until DURATION_S. The ramp is deliberate: under the pool fault the pool-wait
alarm fires within a minute, but errors begin only about 4 minutes in, so
whoever investigates has to diagnose from the early signs, as the runbook
says. sim/pool_sim.py replays this same schedule.

Each request goes out on a worker thread, so a slow response never holds up
the ones behind it: under the pool fault about 50 are in flight at the peak.
Like the generator, it counts failures and never raises them.
"""

import random
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any

from traffic import mix
from traffic.generator import Send

START_RATE = 6.0  # requests per second
PEAK_RATE = 22.0
RAMP_S = 300
DURATION_S = 360  # the ramp, then a minute at the peak
# More than checkout-api's concurrency cap of 80, so the cap, not the burst,
# limits how many requests are in flight.
WORKERS = 100


def rate(t: float) -> float:
    """Requests per second, t seconds into the burst."""
    return START_RATE + (PEAK_RATE - START_RATE) * min(t / RAMP_S, 1.0)


def schedule(rng: random.Random) -> list[float]:
    """When each request goes out, in seconds from the start: a Poisson process at
    the peak rate, thinned to follow the ramp."""
    times = []
    t = 0.0
    while (t := t + rng.expovariate(PEAK_RATE)) < DURATION_S:
        if rng.random() < rate(t) / PEAK_RATE:
            times.append(t)
    return times


class _Tally:
    """The outcomes so far, updated from every worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.outcomes: Counter[str] = Counter()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.max_late_s = 0.0

    def sending(self, late_s: float) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.max_late_s = max(self.max_late_s, late_s)

    def done(self, outcome: str) -> None:
        with self._lock:
            self.in_flight -= 1
            self.outcomes[outcome] += 1


def run(
    send: Send,
    times: Sequence[float],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    rng: random.Random,
    executor: Executor | None = None,
) -> dict[str, Any]:
    """Send a request from the mix at each of `times`, in seconds from now, and count
    the outcomes once every response is in. max_late_s is the most any request went
    out behind its time, waiting for CPU or for a free worker."""
    started = clock()
    tally = _Tally()

    def deliver(due: float, call: mix.Call) -> None:
        tally.sending(clock() - due)
        try:
            outcome = send(call)
        except Exception as exc:  # counted like any other failure; the burst goes on
            outcome = type(exc).__name__
        tally.done(outcome)

    with executor or ThreadPoolExecutor(max_workers=WORKERS) as workers:
        for at in times:
            due = started + at
            sleep(max(0.0, due - clock()))
            workers.submit(deliver, due, mix.sample(rng))
    # Leaving the block waited for the last response.
    return {
        "requests": sum(tally.outcomes.values()),
        "outcomes": dict(sorted(tally.outcomes.items())),
        "elapsed_s": round(clock() - started, 1),
        "max_late_s": round(tally.max_late_s, 3),
        "peak_in_flight": tally.peak_in_flight,
    }
