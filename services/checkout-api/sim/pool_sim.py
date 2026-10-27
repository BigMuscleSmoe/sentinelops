"""The pool fault end to end, in virtual time.

Runs the real App through steady traffic and a burst on a virtual clock, with
an in-memory ledger in place of DynamoDB, and evaluates the alarms the way
CloudWatch will: 10-second periods, ALARM once 2 of the last 3 breach.

Arrivals are handled one at a time, in order, each to completion. That gives
the same result as running them concurrently: a request touches shared state
only when it asks for a connection, which in virtual time is the moment it
arrives, and after that its sleeps move only its own clock.

tests/test_pool_ordering.py asserts on these runs. For a timeline per period:
    uv run python -m sim.pool_sim
run(..., racy=True) shows why the ledger's updates are conditional: without
the conditions, racing requests share connections, and the fault never shows.
"""

import json
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from checkout import VERSION
from checkout.app import App
from checkout.config import Config
from sim import releases
from sim.backend import Backend
from sim.pool import MAX_ATTEMPTS, PoolState, Reservation, applied, holds, plan_reservation, reserved
from traffic import mix

ROUND_TRIP_S = 0.010  # one DynamoDB call from Lambda; the real one is measured in Phase 1, evening 3
BREAK_AT = 1_800_000_000.0  # virtual epoch seconds; positive, because Windows can't format earlier ones
PERIOD_S = 10
BURST_START_RATE = 6.0  # requests per second
BURST_PEAK_RATE = 22.0
BURST_RAMP_S = 300
BURST_S = 360  # the ramp, then a minute at the peak


@dataclass(frozen=True)
class Scenario:
    pool_max_size: int  # from the break on; 20 is the healthy setting
    burst: bool
    horizon_s: int  # how long to run after the break
    release: str = VERSION  # the code that runs from the break on


POOL_FAULT = Scenario(pool_max_size=2, burst=True, horizon_s=480)
SMALL_POOL_NO_BURST = Scenario(pool_max_size=2, burst=False, horizon_s=600)
HEALTHY_BURST = Scenario(pool_max_size=20, burst=True, horizon_s=480)
HEALTHY_BASELINE = Scenario(pool_max_size=20, burst=False, horizon_s=7200)
BAD_DEPLOY = Scenario(pool_max_size=20, burst=True, horizon_s=480, release="1.43.0")


def p99(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.99 * len(ordered)) - 1]


def average(values: Sequence[float]) -> float:
    return sum(values) / len(values)


@dataclass(frozen=True)
class Alarm:
    name: str
    metric: str
    statistic: Callable[[Sequence[float]], float]
    threshold: float


# The alarms the stack will define: each breaches above its threshold.
ALARMS = (
    Alarm("pool-wait", "pool_wait_time_ms", p99, 100.0),
    Alarm("errors", "error", average, 0.05),
    Alarm("latency", "latency_ms", p99, 1500.0),
)


class VirtualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class MemoryLedger:
    """DynamoLedger's protocol, in memory on the virtual clock.

    The read and each update take a round trip. The read sees the updates that
    landed before it began; an update is checked against every update that came
    before it, and when its condition fails the request plans again from the
    pool as it is now, as DynamoDB's ALL_OLD return lets the real one do. With
    racy=True updates are unconditional and set what the request planned.
    """

    def __init__(
        self,
        clock: VirtualClock,
        rng: random.Random,
        round_trip_s: float = ROUND_TRIP_S,
        racy: bool = False,
    ) -> None:
        self._clock = clock
        self._rng = rng
        self._round_trip_s = round_trip_s
        self._racy = racy
        self._updates: list[tuple[float, PoolState]] = [(-math.inf, PoolState())]  # (landed at, pool after)

    def reserve(self, max_size: int, timeout_s: float, hold_s: float, after_release_s: float) -> Reservation:
        now = self._clock()
        landed = (state for landed_at, state in reversed(self._updates) if landed_at <= now)
        pool = next(landed, self._updates[0][1])
        self._clock.sleep(self._round_trip_s)
        for _ in range(MAX_ATTEMPTS):
            plan = plan_reservation(pool, now, max_size, timeout_s, hold_s, after_release_s, self._rng)
            self._clock.sleep(self._round_trip_s)
            current = self._updates[-1][1]
            if self._racy:
                plan = replace(plan, queue=False)  # set the planned release, whatever is there now
            elif not holds(plan, current):
                pool = current
                continue
            after, release_at = applied(plan, current)
            self._updates.append((self._clock(), after))
            del self._updates[:-64]
            return reserved(plan, release_at)
        return reserved(plan, None)


def burst_rate(t: float) -> float:
    return BURST_START_RATE + (BURST_PEAK_RATE - BURST_START_RATE) * min(t / BURST_RAMP_S, 1.0)


def arrivals(scenario: Scenario, rng: random.Random) -> list[float]:
    """Arrival times relative to the break: steady traffic from a minute before it,
    plus the burst from the break on."""
    every_s = 60 / mix.REQUESTS_PER_MINUTE
    steps = int((scenario.horizon_s + 60) / every_s)
    times = [-60 + i * every_s + rng.uniform(-1.0, 1.0) for i in range(steps)]
    if scenario.burst:  # a Poisson process, thinned to follow the ramp
        t = 0.0
        while (t := t + rng.expovariate(BURST_PEAK_RATE)) < BURST_S:
            if rng.random() < burst_rate(t) / BURST_PEAK_RATE:
                times.append(t)
    return sorted(times)


def request_event(rng: random.Random) -> dict[str, Any]:
    """One request from the generator's mix, as the HTTP API event checkout-api gets."""
    call = mix.sample(rng)
    if call.body is None:
        return {"routeKey": call.route, "pathParameters": dict(call.path_parameters)}
    return {"routeKey": call.route, "body": json.dumps(call.body), "isBase64Encoded": False}


def run(
    scenario: Scenario, seed: int, round_trip_s: float = ROUND_TRIP_S, racy: bool = False
) -> list[dict[str, Any]]:
    """Every request's log line, parsed, in arrival order."""
    rng = random.Random(seed)
    clock = VirtualClock()
    healthy = Config()
    broken = replace(healthy, pool_max_size=scenario.pool_max_size, version=scenario.release)
    current = healthy
    lines: list[str] = []
    app = App(
        settings=lambda: current,
        backend=Backend(MemoryLedger(clock, random.Random(seed + 1), round_trip_s, racy), rng),
        clock=clock,
        sleep=clock.sleep,
        emit=lines.append,
    )
    try:
        for n, at in enumerate(arrivals(scenario, rng)):
            current = broken if at >= 0 else healthy
            releases.deploy(current.version)
            clock.now = BREAK_AT + at
            app.handle(request_event(rng), request_id=f"sim-{n:06d}", cold_start=False)
    finally:
        releases.deploy(VERSION)
    return [json.loads(line) for line in lines]


def declared(line: dict[str, Any]) -> set[str]:
    """The metrics a log line declares in its EMF block."""
    directives = line.get("_aws", {}).get("CloudWatchMetrics", [])
    return {metric["Name"] for directive in directives for metric in directive["Metrics"]}


def periods(lines: list[dict[str, Any]], metric: str) -> dict[int, list[float]]:
    """A metric's values in each 10-second period, keyed by the period's start
    relative to the break. A datapoint lands in the period of its EMF timestamp."""
    values: dict[int, list[float]] = {}
    for line in lines:
        if metric in declared(line):
            at = line["_aws"]["Timestamp"] / 1000 - BREAK_AT
            values.setdefault(math.floor(at / PERIOD_S) * PERIOD_S, []).append(line[metric])
    return values


def first_alarm(lines: list[dict[str, Any]], alarm: Alarm, start_s: int, end_s: int) -> int | None:
    """When the alarm would first go to ALARM, in seconds after the break: the end of
    the first period that makes 2 of the last 3 breach. Missing data doesn't breach."""
    values = periods(lines, alarm.metric)
    breaching: list[bool] = []
    for start in range(start_s, end_s, PERIOD_S):
        datapoints = values.get(start)
        breaching.append(bool(datapoints) and alarm.statistic(datapoints) > alarm.threshold)
        if sum(breaching[-3:]) >= 2:
            return start + PERIOD_S
    return None


def first_alarms(lines: list[dict[str, Any]], start_s: int, end_s: int) -> dict[str, int | None]:
    return {alarm.name: first_alarm(lines, alarm, start_s, end_s) for alarm in ALARMS}


def report(name: str, scenario: Scenario, seed: int, racy: bool = False) -> None:
    lines = run(scenario, seed, racy=racy)
    after = [line for line in lines if line["_aws"]["Timestamp"] / 1000 >= BREAK_AT]
    print(f"\n== {name}, seed {seed}: {len(after)} requests after the break")
    print("first ALARM, s after the break:", first_alarms(lines, -60, scenario.horizon_s))
    print("    t     n  wait_p99  lat_p99   err%  active  concurrent")
    columns = {
        metric: periods(lines, metric)
        for metric in ("pool_wait_time_ms", "latency_ms", "error", "pool_active_connections", "concurrent_requests")
    }
    for start in range(-60, scenario.horizon_s, 20):
        latency = columns["latency_ms"].get(start)
        if not latency:
            continue
        wait = columns["pool_wait_time_ms"].get(start) or [0.0]
        print(
            f"{start:5d} {len(latency):5d} {p99(wait):9.0f} {p99(latency):8.0f} "
            f"{100 * average(columns['error'][start]):6.1f} "
            f"{max(columns['pool_active_connections'].get(start) or [0]):7.0f} "
            f"{max(columns['concurrent_requests'].get(start) or [0]):11.0f}"
        )


if __name__ == "__main__":
    for seed in (1, 2, 3):
        report("pool fault: POOL_MAX_SIZE 20 -> 2 and the burst", POOL_FAULT, seed)
    report("small pool, steady traffic only", SMALL_POOL_NO_BURST, 4)
    report("healthy pool and the burst", HEALTHY_BURST, 5)
    report("pool fault again, with unconditional ledger updates", POOL_FAULT, 1, racy=True)
    report("bad deploy: release 1.43.0 and the burst", BAD_DEPLOY, 1)
