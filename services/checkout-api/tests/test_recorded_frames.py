"""Every stack the recorded windows hold is the one this code raises, frame for frame.

The windows (tools/tests/fixtures/windows/) are the live service's own log
lines, and a frame names a file and a line: checkout/pricing.py:15 in
apply_promo. The read tool shows the release's code numbered line by line, so
a change that moves a line under a recorded frame would show the agent a line
the frame doesn't mean. Each distinct recorded stack is raised again here, the
way the live fault raised it, and must come back identical.
"""

import gzip
import json
from pathlib import Path

import pytest

from checkout import VERSION
from checkout.app import App
from checkout.backend import Timeline
from checkout.config import Config
from sim import releases
from sim.pool_sim import VirtualClock

WINDOWS = Path(__file__).resolve().parents[3] / "tools" / "tests" / "fixtures" / "windows"
START = 1_800_000_000.0
ITEMS = [{"product_id": "p-1001", "quantity": 1}]


def recorded_stacks() -> dict[tuple[str, str, tuple[str, ...]], int]:
    """Each distinct (version, route, stack) in every window, with how many lines carry it."""
    stacks: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for path in sorted(WINDOWS.glob("*/logs.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for raw in f:
                line = json.loads(json.loads(raw)["message"])
                if "stack" in line:
                    key = (line["version"], line["route"], tuple(line["stack"]))
                    stacks[key] = stacks.get(key, 0) + 1
    return stacks


class Pool:
    """A pool with every connection taken: the request waits out the acquire timeout."""

    def __init__(self, timed_out: bool) -> None:
        self.clock = VirtualClock(START)
        self.timed_out = timed_out

    def connect(self, route: str, settings: Config) -> Timeline:
        return Timeline(
            started_at=self.clock(),
            wait_s=settings.pool_acquire_timeout_s if self.timed_out else 0.0,
            timed_out=self.timed_out,
            hold_s=0.05,
            upstream_s=0.08 if route == "POST /checkout" else None,
            active_connections=settings.pool_max_size,
            concurrent_requests=1,
        )


def raised_stack(version: str, route: str) -> tuple[str, ...]:
    """The stack this code logs for the fault the recorded line's version and route point to."""
    pool = Pool(timed_out=version == VERSION)  # 1.42.0's failures are pool timeouts; 1.43.0's are its own
    events = {
        "GET /products/{id}": {"routeKey": route, "pathParameters": {"id": "p-1001"}},
        "POST /cart/items": {"routeKey": route, "body": json.dumps({"product_id": "p-1001", "quantity": 1})},
        "POST /checkout": {"routeKey": route, "body": json.dumps({"items": ITEMS})},  # no promo code
    }
    lines: list[str] = []
    settings = Config(version=version)
    app = App(settings=lambda: settings, backend=pool, clock=pool.clock, sleep=pool.clock.sleep, emit=lines.append)
    releases.deploy(version)
    try:
        app.handle(events[route], request_id="frames", cold_start=False)
    finally:
        releases.deploy(VERSION)
    (line,) = lines
    return tuple(json.loads(line)["stack"])


RECORDED = recorded_stacks()


def test_the_windows_hold_both_faults_stacks_on_every_route_they_failed():
    # Guards the test below against passing on nothing: 3 pool stacks, 1 bad deploy's.
    assert sorted((version, route) for version, route, _ in RECORDED) == [
        ("1.42.0", "GET /products/{id}"),
        ("1.42.0", "POST /cart/items"),
        ("1.42.0", "POST /checkout"),
        ("1.43.0", "POST /checkout"),
    ]
    assert sum(RECORDED.values()) > 1000


@pytest.mark.parametrize(("version", "route", "stack"), sorted(RECORDED), ids=lambda v: str(v)[:40])
def test_each_recorded_stack_is_raised_again_frame_for_frame(version, route, stack):
    assert raised_stack(version, route) == stack
