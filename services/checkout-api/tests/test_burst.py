"""The burst: its schedule, and how it paces, overlaps, and counts its requests."""

import importlib
import json
import random
import sys
import threading
import time
import types
from collections import Counter
from concurrent.futures import Executor, Future

import pytest

from sim.pool_sim import VirtualClock
from traffic import burst, mix

START = 1_800_000_000.0
SEEDS = (1, 2, 3)


class Serial(Executor):
    """Sends each request as it's submitted, so the virtual clock says exactly when."""

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def test_the_rate_ramps_from_6_to_22_a_second_over_5_minutes_then_holds():
    assert [burst.rate(t) for t in (0, 150, 300, 359)] == [6.0, 14.0, 22.0, 22.0]


@pytest.mark.parametrize("seed", SEEDS)
def test_the_schedule_follows_the_ramp_for_6_minutes(seed):
    times = burst.schedule(random.Random(seed))
    assert times == sorted(times) and 0 < times[0] and times[-1] < burst.DURATION_S
    per_minute = Counter(int(t // 60) for t in times)
    # The rate's integral over each minute: 456 requests in the first, 1,320 in the last.
    expected = [456, 648, 840, 1032, 1224, 1320]
    assert [per_minute[minute] for minute in range(6)] == pytest.approx(expected, rel=0.15)


def test_each_request_goes_out_at_its_time_from_the_mix():
    clock = VirtualClock(START)
    sent: list[tuple[float, str]] = []

    def send(call):
        sent.append((clock(), call.route))
        return "200"

    times = [0.0, 0.25, 0.5, 2.0]
    summary = burst.run(send, times, clock, clock.sleep, random.Random(1), Serial())
    assert [at for at, _ in sent] == pytest.approx([START + t for t in times])
    assert {route for _, route in sent} <= set(mix.ROUTE_MIX)
    assert summary == {"requests": 4, "outcomes": {"200": 4}, "elapsed_s": 2.0, "max_late_s": 0.0, "peak_in_flight": 1}


def test_a_request_held_up_by_a_slow_one_shows_as_late():
    clock = VirtualClock(START)
    slow = [1.5]  # the first response takes 1.5 s, and Serial has no other worker

    def send(call):
        clock.sleep(slow.pop() if slow else 0.0)
        return "200"

    summary = burst.run(send, [0.0, 0.5, 1.0, 3.0], clock, clock.sleep, random.Random(1), Serial())
    assert summary["max_late_s"] == 1.0  # the second went out at 1.5 s, not 0.5 s
    assert summary["elapsed_s"] == 3.0  # and the fourth on time


def test_requests_overlap_so_a_slow_response_holds_up_nothing():
    n = 8
    together = threading.Barrier(n, timeout=5)

    def send(call):
        together.wait()  # passes only once all n are in flight at the same time
        return "200"

    summary = burst.run(send, [0.0] * n, time.time, time.sleep, random.Random(1))
    assert summary["outcomes"] == {"200": n}
    assert summary["peak_in_flight"] == n


def test_failures_are_counted_not_raised():
    outcomes = iter(["200", RuntimeError("signing failed"), "500", "URLError"])

    def send(call):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    clock = VirtualClock(START)
    summary = burst.run(send, [0.0, 1.0, 2.0, 3.0], clock, clock.sleep, random.Random(1), Serial())
    assert summary["outcomes"] == {"200": 1, "500": 1, "RuntimeError": 1, "URLError": 1}
    assert summary["requests"] == 4


def stub_module(monkeypatch, name: str, **attributes) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)


def test_the_lambda_entry_point_sends_a_schedule_and_prints_one_line(monkeypatch, capsys):
    # The sender is built at import, from botocore, which comes with the Lambda runtime.
    stub_module(monkeypatch, "botocore")
    stub_module(monkeypatch, "botocore.auth", SigV4Auth=None)
    stub_module(monkeypatch, "botocore.awsrequest", AWSRequest=None)
    stub_module(
        monkeypatch, "botocore.session", get_session=lambda: types.SimpleNamespace(get_credentials=lambda: None)
    )
    monkeypatch.setenv("CHECKOUT_API_URL", "https://api.invalid")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delitem(sys.modules, "traffic.handler", raising=False)
    module = importlib.import_module("traffic.handler")
    try:
        monkeypatch.setattr(module, "_send", lambda call: "200")
        monkeypatch.setattr(burst, "schedule", lambda rng: [0.0, 0.01])
        summary = module.burst_handler({}, None)
        (line,) = capsys.readouterr().out.splitlines()
        assert json.loads(line) == {"message": "burst sent", **summary}
        assert summary["outcomes"] == {"200": 2}
    finally:
        sys.modules.pop("traffic.handler", None)
