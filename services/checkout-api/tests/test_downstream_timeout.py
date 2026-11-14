"""The downstream timeout, end to end in the harness: payments-api slows down,
and about one checkout in 25 passes the 2 s client timeout.

Its signature is neither the pool fault's nor the bad deploy's. Only the
latency alarm fires, on the p99 of every route together (the alarm, like the
metric, has no route dimension); pool wait stays at zero, and so does the
error rate, which counts only this service's own 500s. The slowness is all in
checkout's call to payments-api: the other routes stay fast, the pool and the
database are untouched, and the 504s name the upstream, with no stack.
"""

import re
import statistics
from pathlib import Path

import pytest

from checkout import VERSION
from sim.pool_sim import (
    ALARMS,
    BREAK_AT,
    DOWNSTREAM_TIMEOUT,
    FAULT_S,
    HEALTHY_BURST,
    average,
    first_alarm,
    first_alarms,
    p99,
    periods,
    run,
)
from traffic.burst import DURATION_S as BURST_S

SEEDS = (1, 2, 3)
CHECKOUT = "POST /checkout"
FIRST_ALARM_S = 30  # as test_pool_ordering.py: within the 60 s the demo promises, live
ALARMS_TF = Path(__file__).resolve().parents[3] / "infra" / "checkout_alarms.tf"


@pytest.fixture(scope="module")
def runs():
    return {seed: run(DOWNSTREAM_TIMEOUT, seed) for seed in SEEDS}


def at(line: dict) -> float:
    return line["_aws"]["Timestamp"] / 1000 - BREAK_AT


def during(lines: list[dict]) -> list[dict]:
    return [line for line in lines if 0 <= at(line) < FAULT_S]


def test_the_harness_alarms_are_the_deployed_ones():
    """Metric, statistic, and threshold as infra/checkout_alarms.tf sets them, none with a dimension."""
    text = ALARMS_TF.read_text(encoding="utf-8")
    deployed = {
        name: (metric, statistic, float(threshold))
        for name, metric, statistic, threshold in re.findall(
            r'([\w-]+) = \{\s*metric\s*= "(\w+)"\s*statistic\s*= "(\w+)"\s*threshold\s*= ([\d.]+)', text
        )
    }
    statistics_named = {"p99": p99, "Average": average}
    assert set(deployed) == {alarm.name for alarm in ALARMS}
    for alarm in ALARMS:
        metric, statistic, threshold = deployed[alarm.name]
        assert (alarm.metric, alarm.threshold) == (metric, threshold)
        assert alarm.statistic is statistics_named[statistic]
    assert "dimensions" not in text.split("resource", 1)[1]


@pytest.mark.parametrize("seed", SEEDS)
def test_only_the_all_routes_latency_alarm_fires_and_within_30_s(runs, seed):
    alarms = first_alarms(runs[seed], -60, DOWNSTREAM_TIMEOUT.horizon_s)
    assert alarms["latency"] is not None and 0 < alarms["latency"] <= FIRST_ALARM_S
    assert (alarms["pool-wait"], alarms["errors"]) == (None, None)
    # The latency alarm reads every route's lines; checkouts are a fifth of them,
    # and the alarm fires only because of them.
    latency = next(alarm for alarm in ALARMS if alarm.name == "latency")
    routes = {line["route"] for line in runs[seed] if latency.metric in line}
    assert routes == {"GET /products/{id}", "POST /cart/items", CHECKOUT}
    without_checkouts = [line for line in runs[seed] if line["route"] != CHECKOUT]
    assert first_alarm(without_checkouts, latency, -60, DOWNSTREAM_TIMEOUT.horizon_s) is None


@pytest.mark.parametrize("seed", SEEDS)
def test_checkouts_are_a_fifth_of_the_traffic_and_the_only_slow_route(runs, seed):
    lines = during(runs[seed])
    checkouts = [line for line in lines if line["route"] == CHECKOUT]
    assert 0.18 <= len(checkouts) / len(lines) <= 0.22  # traffic/mix.py's 20%
    others = [line["latency_ms"] for line in lines if line["route"] != CHECKOUT]
    assert p99([line["latency_ms"] for line in checkouts]) > 2000
    assert p99(others) < 300


@pytest.mark.parametrize("seed", SEEDS)
def test_every_failure_is_the_same_504_naming_payments_api(runs, seed):
    lines = runs[seed]
    failed = [line for line in lines if line["status"] >= 500]
    checkouts = [line for line in during(lines) if line["route"] == CHECKOUT]
    assert 0.025 <= len(failed) / len(checkouts) <= 0.055  # about 4.0%
    assert not any(line["error"] for line in lines)  # the error rate counts 500s only
    signatures = {
        (line["route"], line["status"], line["level"], line["version"], line["upstream_timeout"], line["message"])
        for line in failed
    }
    assert signatures == {
        (CHECKOUT, 504, "ERROR", VERSION, 1, "UpstreamTimeout: payments-api did not respond within 2000 ms")
    }
    assert not any("stack" in line for line in failed)
    assert all(1999 <= line["upstream_latency_ms"] <= 2001 for line in failed)  # recorded at the timeout


def test_the_504s_come_with_and_without_a_promo_code(runs):
    failed = [line for seed in SEEDS for line in runs[seed] if line["status"] == 504]
    assert {line["has_promo"] for line in failed} == {True, False}


@pytest.mark.parametrize("seed", SEEDS)
def test_the_pool_and_the_database_are_untouched(runs, seed):
    lines = during(runs[seed])
    healthy = during(run(HEALTHY_BURST, seed))
    assert max(line.get("pool_wait_time_ms", 0) for line in lines) == 0
    assert max(line.get("pool_active_connections", 0) for line in lines) < DOWNSTREAM_TIMEOUT.pool_max_size
    query = statistics.median(line["db_query_ms"] for line in lines if "db_query_ms" in line)
    baseline = statistics.median(line["db_query_ms"] for line in healthy if "db_query_ms" in line)
    assert 0.85 <= query / baseline <= 1.15


@pytest.mark.parametrize("seed", SEEDS)
def test_payments_latency_shows_the_cause_and_timeouts_come_all_through_the_burst(runs, seed):
    lines = runs[seed]
    upstream = periods(lines, "upstream_latency_ms")
    burst = [start for start in upstream if 0 <= start < BURST_S]
    assert statistics.median(p99(upstream[start]) for start in burst) > 1500
    timeouts = periods(lines, "upstream_timeout")
    with_one = [start for start in burst if sum(timeouts.get(start, [])) > 0]
    assert len(with_one) >= len(burst) / 3
    healthy = periods(run(HEALTHY_BURST, seed), "upstream_latency_ms")
    assert statistics.median(p99(healthy[start]) for start in healthy if 0 <= start < BURST_S) < 600


@pytest.mark.parametrize("seed", SEEDS)
def test_nothing_is_slow_or_fails_once_the_fault_ends(runs, seed):
    lines = runs[seed]
    after = [line for line in lines if at(line) >= FAULT_S + 5]
    assert after and not any(line["status"] >= 500 for line in after)
    latency = next(alarm for alarm in ALARMS if alarm.name == "latency")
    assert first_alarm(lines, latency, FAULT_S + 10, DOWNSTREAM_TIMEOUT.horizon_s) is None
