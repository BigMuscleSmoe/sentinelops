"""The pool fault trips its alarms in the order the runbook describes.

runbooks/db-pool-exhaustion.md tells the responder that pool wait and p99
latency move minutes before the error rate does, and the agent is expected to
diagnose from those early signals. These tests drive the real service through
the harness in sim/pool_sim.py and hold the simulation to that story.
"""

import statistics

import pytest

from sim.pool_sim import (
    BURST_S,
    HEALTHY_BASELINE,
    HEALTHY_BURST,
    POOL_FAULT,
    SMALL_POOL_NO_BURST,
    first_alarms,
    periods,
    run,
)

SEEDS = (1, 2, 3)
QUIET = {"pool-wait": None, "errors": None, "latency": None}


@pytest.fixture(scope="module")
def fault_runs():
    return {seed: run(POOL_FAULT, seed) for seed in SEEDS}


def overloaded(lines: list[dict]) -> list[int]:
    """The periods of the burst in which more than 5% of requests failed."""
    errors = periods(lines, "error")
    return [start for start, values in errors.items() if 0 <= start < BURST_S and statistics.mean(values) > 0.05]


@pytest.mark.parametrize("seed", SEEDS)
def test_pool_wait_alarms_first_then_latency_then_errors(fault_runs, seed):
    alarms = first_alarms(fault_runs[seed], 0, POOL_FAULT.horizon_s)
    assert alarms["pool-wait"] is not None and alarms["pool-wait"] <= 40
    assert alarms["errors"] is not None
    assert 180 <= alarms["errors"] - alarms["pool-wait"] <= 360  # the runbook's 3 to 6 minutes
    assert alarms["latency"] is not None and alarms["latency"] <= alarms["errors"]


@pytest.mark.parametrize("seed", SEEDS)
def test_the_pool_is_pegged_while_concurrency_far_exceeds_it(fault_runs, seed):
    lines = fault_runs[seed]
    active = periods(lines, "pool_active_connections")
    concurrent = periods(lines, "concurrent_requests")
    overload = overloaded(lines)
    assert overload
    assert all(max(active[start]) == POOL_FAULT.pool_max_size for start in overload)
    assert max(max(concurrent[start]) for start in overload) >= 10 * POOL_FAULT.pool_max_size


@pytest.mark.parametrize("seed", SEEDS)
def test_query_time_stays_flat_while_the_wait_grows(fault_runs, seed):
    lines = fault_runs[seed]
    queries = periods(lines, "db_query_ms")
    overload = set(overloaded(lines))
    early = statistics.median(v for start, values in queries.items() if 0 <= start < 120 for v in values)
    late = statistics.median(v for start, values in queries.items() if start in overload for v in values)
    assert 0.85 <= late / early <= 1.15


@pytest.mark.parametrize("seed", SEEDS)
def test_errors_stop_when_the_burst_does(fault_runs, seed):
    errors = periods(fault_runs[seed], "error")
    after = [statistics.mean(values) for start, values in errors.items() if start >= BURST_S + 20]
    assert after and max(after) == 0


@pytest.mark.parametrize("round_trip_s", [0.0, 0.040])
def test_the_round_trip_time_does_not_change_the_story(round_trip_s):
    alarms = first_alarms(run(POOL_FAULT, 1, round_trip_s=round_trip_s), 0, POOL_FAULT.horizon_s)
    assert alarms["pool-wait"] <= 40
    assert 180 <= alarms["errors"] - alarms["pool-wait"] <= 360


def test_without_conditional_updates_the_fault_would_not_show():
    lines = run(POOL_FAULT, 1, round_trip_s=0.020, racy=True)
    assert first_alarms(lines, 0, POOL_FAULT.horizon_s)["errors"] is None


@pytest.mark.parametrize(
    "scenario",
    [SMALL_POOL_NO_BURST, HEALTHY_BURST, HEALTHY_BASELINE],
    ids=["small pool, steady traffic", "healthy pool, burst", "healthy pool, 2 h of steady traffic"],
)
def test_it_takes_both_the_small_pool_and_the_burst(scenario):
    lines = run(scenario, 7)
    assert first_alarms(lines, -60, scenario.horizon_s) == QUIET
    assert max(line.get("pool_wait_time_ms", 0) for line in lines) == 0
    assert sum(line["error"] for line in lines) == 0
