"""The bad deploy, end to end in the harness: 1.43.0 fails every checkout without
a promo code, from its first request.

Its signature is the reverse of the pool fault's: the error alarm fires at
once, latency and pool wait stay flat, and every failure is the same TypeError
on /checkout.
"""

import pytest

from checkout import VERSION
from sim.pool_sim import BAD_DEPLOY, first_alarms, run

SEEDS = (1, 2, 3)


@pytest.fixture(scope="module")
def deploy_runs():
    return {seed: run(BAD_DEPLOY, seed) for seed in SEEDS}


@pytest.mark.parametrize("seed", SEEDS)
def test_only_the_error_alarm_fires_and_within_30_s(deploy_runs, seed):
    alarms = first_alarms(deploy_runs[seed], -60, BAD_DEPLOY.horizon_s)
    assert alarms["errors"] is not None and 0 < alarms["errors"] <= 30
    assert (alarms["pool-wait"], alarms["latency"]) == (None, None)


@pytest.mark.parametrize("seed", SEEDS)
def test_about_one_request_in_seven_fails_all_the_same_way_and_fast(deploy_runs, seed):
    after = [line for line in deploy_runs[seed] if line["version"] == "1.43.0"]
    failed = [line for line in after if line["error"]]
    assert 0.10 <= len(failed) / len(after) <= 0.18  # 20% checkouts x 70% without a promo
    signatures = {
        (line["route"], line["error_type"], line["has_promo"], line["stack"][-1].split(":")[0])
        for line in failed
    }
    assert signatures == {("POST /checkout", "TypeError", False, "checkout/pricing.py")}
    assert all(line["stack"][-1].endswith(" in apply_promo") for line in failed)
    assert max(line["latency_ms"] for line in failed) < 10


def test_nothing_fails_before_the_deploy(deploy_runs):
    before = [line for line in deploy_runs[1] if line["version"] == VERSION]
    assert before and not any(line["error"] for line in before)
