"""The traffic mix, and how the generator paces and counts its requests."""

import random
import urllib.error
import urllib.request
from collections import Counter

import pytest

from checkout.app import PROMOS
from checkout.catalog import PRODUCTS
from sim.pool_sim import HEALTHY_BURST, VirtualClock, run
from traffic import generator, mix

START = 1_800_000_000.0


def test_the_mix_uses_checkout_apis_own_products_and_promo_codes():
    assert set(mix.PRODUCT_IDS) == set(PRODUCTS)
    assert set(mix.PROMO_CODES) == set(PROMOS)
    assert mix.UNKNOWN_PRODUCT_ID not in PRODUCTS


def test_the_mix_has_the_planned_shares():
    rng = random.Random(1)
    calls = [mix.sample(rng) for _ in range(20_000)]
    routes = Counter(call.route for call in calls)
    for route, share in mix.ROUTE_MIX.items():
        assert routes[route] / len(calls) == pytest.approx(share, abs=0.02)
    checkouts = [call for call in calls if call.route == "POST /checkout"]
    with_promo = sum("promo_code" in call.body for call in checkouts) / len(checkouts)
    assert with_promo == pytest.approx(mix.PROMO_SHARE, abs=0.03)


def test_each_call_is_addressed_as_its_route():
    rng = random.Random(2)
    for call in (mix.sample(rng) for _ in range(500)):
        method, template = call.route.split(" ")
        assert call.method == method
        assert call.path == template.replace("{id}", call.path_parameters.get("id", ""))
        assert (call.body is None) == (method == "GET")


def test_checkout_api_accepts_every_call_in_the_mix():
    statuses = Counter(line["status"] for line in run(HEALTHY_BURST, 11))
    assert set(statuses) == {200, 201, 404}  # no 400s: every body is well formed
    assert statuses[404] / sum(statuses.values()) < 0.02


def test_a_run_spreads_12_requests_over_about_50_s():
    clock = VirtualClock(START)
    sent: list[float] = []

    def send(call):
        sent.append(clock())
        return "200"

    summary = generator.run(send, clock, clock.sleep, random.Random(3))
    assert (summary["requests"], summary["outcomes"]) == (12, {"200": 12})
    assert 45 <= sent[-1] - START <= 48 and summary["elapsed_s"] == round(sent[-1] - START, 1)
    assert min(later - earlier for earlier, later in zip(sent, sent[1:])) > 3


def test_a_slow_response_never_shifts_the_later_requests():
    clock = VirtualClock(START)
    sent: list[float] = []

    def send(call):
        sent.append(clock())
        clock.sleep(10.0 if len(sent) == 1 else 0.1)
        return "200"

    generator.run(send, clock, clock.sleep, random.Random(3))
    assert sent[-1] - START <= 48


def test_failures_are_counted_not_raised():
    clock = VirtualClock(START)
    outcomes = iter(["200", "500", "URLError", "201"] * 3)
    summary = generator.run(lambda call: next(outcomes), clock, clock.sleep, random.Random(5))
    assert summary["outcomes"] == {"200": 3, "201": 3, "500": 3, "URLError": 3}


@pytest.mark.parametrize(
    "error, outcome",
    [
        (urllib.error.HTTPError("https://api.invalid/checkout", 500, "Internal Server Error", {}, None), "500"),
        (urllib.error.URLError("no route to host"), "URLError"),
        (TimeoutError("timed out"), "TimeoutError"),
    ],
)
def test_deliver_reports_an_error_response_or_no_response(monkeypatch, error, outcome):
    def urlopen(request, timeout):
        assert timeout == generator.TIMEOUT_S
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert generator.deliver(urllib.request.Request("https://api.invalid/checkout")) == outcome
