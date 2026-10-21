import base64
import json
import tomllib
from pathlib import Path

import pytest

from checkout import VERSION
from checkout.app import App
from checkout.config import Config
from sim.backend import Timeline
from sim.pool_sim import VirtualClock

START = 1_800_000_000.0
REQUEST_ID = "5f0c1d2e-0000-4000-8000-000000000000"
ITEMS = [{"product_id": "p-1001", "quantity": 1}, {"product_id": "p-1006", "quantity": 2}]


class FixedBackend:
    """Gives every request the same timeline, starting when it asks."""

    def __init__(
        self, wait_s=0.0, timed_out=False, hold_s=0.05, upstream_s=0.08, active=1, concurrent=1
    ) -> None:
        self.timeline = dict(
            wait_s=wait_s,
            timed_out=timed_out,
            hold_s=hold_s,
            active_connections=active,
            concurrent_requests=concurrent,
        )
        self.upstream_s = upstream_s
        self.clock = VirtualClock(START)
        self.routes: list[str] = []

    def connect(self, route: str, settings: Config) -> Timeline:
        self.routes.append(route)
        upstream_s = self.upstream_s if route == "POST /checkout" else None
        return Timeline(started_at=self.clock(), upstream_s=upstream_s, **self.timeline)


class BrokenBackend(FixedBackend):
    def connect(self, route: str, settings: Config) -> Timeline:
        raise RuntimeError("ledger exploded")


def serve(event, backend=None, settings=Config(), cold_start=False):
    backend = backend or FixedBackend()
    clock = backend.clock
    lines: list[str] = []
    app = App(settings=lambda: settings, backend=backend, clock=clock, sleep=clock.sleep, emit=lines.append)
    response = app.handle(event, request_id=REQUEST_ID, cold_start=cold_start)
    (line,) = lines
    return response, json.loads(line)


def get_product(product_id: str) -> dict:
    return {"routeKey": "GET /products/{id}", "pathParameters": {"id": product_id}}


def post(route: str, body, encode: bool = False) -> dict:
    raw = body if isinstance(body, str) else json.dumps(body)
    if encode:
        return {"routeKey": route, "body": base64.b64encode(raw.encode()).decode(), "isBase64Encoded": True}
    return {"routeKey": route, "body": raw, "isBase64Encoded": False}


def declared(line: dict) -> set[str]:
    return {metric["Name"] for d in line["_aws"]["CloudWatchMetrics"] for metric in d["Metrics"]}


def test_health_reports_the_version_and_touches_nothing():
    backend = FixedBackend()
    response, line = serve({"routeKey": "GET /health"}, backend)
    assert response["statusCode"] == 200
    assert response["headers"] == {"content-type": "application/json"}
    assert json.loads(response["body"]) == {"status": "ok", "service": "checkout-api", "version": VERSION}
    assert backend.routes == []
    assert "_aws" not in line
    assert (line["route"], line["status"], line["version"]) == ("GET /health", 200, VERSION)


def test_a_product_lookup_holds_a_connection_for_its_query():
    response, line = serve(get_product("p-1001"), FixedBackend(hold_s=0.05))
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"id": "p-1001", "name": "Trail running shoes", "price_cents": 12900}
    assert (line["latency_ms"], line["db_query_ms"], line["pool_wait_time_ms"]) == (50.0, 50.0, 0.0)
    assert (line["pool_max_size"], line["pool_active_connections"], line["concurrent_requests"]) == (20, 1, 1)
    assert (line["level"], line["error"], line["message"]) == ("INFO", 0, "GET /products/{id} 200")
    assert declared(line) == {
        "error", "latency_ms", "pool_wait_time_ms", "pool_active_connections", "concurrent_requests", "db_query_ms",
    }


def test_an_unknown_product_is_a_404_after_the_query():
    backend = FixedBackend()
    response, line = serve(get_product("p-9999"), backend)
    assert response["statusCode"] == 404
    assert backend.routes == ["GET /products/{id}"]
    assert (line["level"], line["error"]) == ("INFO", 0)
    assert line["message"] == "GET /products/{id} 404: product p-9999 not found"
    assert "error_type" not in line


def test_adding_to_a_cart():
    response, line = serve(post("POST /cart/items", {"product_id": "p-1003", "quantity": 2}))
    assert response["statusCode"] == 201
    assert json.loads(response["body"]) == {"cart_id": "cart-5f0c1d2e", "product_id": "p-1003", "quantity": 2}
    assert line["db_query_ms"] == 50.0


def test_a_base64_body_is_decoded():
    response, _ = serve(post("POST /cart/items", {"product_id": "p-1003", "quantity": 1}, encode=True))
    assert response["statusCode"] == 201


@pytest.mark.parametrize(
    "event",
    [
        post("POST /cart/items", {"product_id": "p-1003", "quantity": 0}),
        post("POST /cart/items", {"product_id": "p-1003", "quantity": 11}),
        post("POST /cart/items", {"product_id": "p-1003", "quantity": "2"}),
        post("POST /cart/items", {"product_id": "p-1003", "quantity": True}),
        post("POST /cart/items", {"product_id": "", "quantity": 1}),
        post("POST /cart/items", {"product_id": ["p-1003"], "quantity": 1}),
        post("POST /cart/items", {"product_id": "p-1003", "quantity": 1, "cart_id": 7}),
        post("POST /cart/items", "not json"),
        post("POST /cart/items", [1, 2]),
        {"routeKey": "POST /cart/items", "body": "%%%", "isBase64Encoded": True},
        post("POST /checkout", {"items": []}),
        post("POST /checkout", {"items": "p-1001"}),
        post("POST /checkout", {"items": ["p-1001"]}),
        post("POST /checkout", {"items": [{"product_id": "p-0000", "quantity": 1}]}),
        post("POST /checkout", {"items": ITEMS * 11}),
        post("POST /checkout", {"items": ITEMS, "promo_code": "FREE"}),
        post("POST /checkout", {"items": ITEMS, "promo_code": 10}),
    ],
)
def test_a_bad_request_is_a_400_before_any_query(event):
    backend = FixedBackend()
    response, line = serve(event, backend)
    assert response["statusCode"] == 400
    assert backend.routes == []
    assert line["error"] == 0
    assert "pool_wait_time_ms" not in line


def test_a_checkout_charges_payments_after_its_connection_is_back():
    body = {"items": ITEMS, "promo_code": "TRAIL10"}
    response, line = serve(post("POST /checkout", body), FixedBackend(hold_s=0.3, upstream_s=0.08))
    assert response["statusCode"] == 200
    # 12900 + 2 * 2200 = 17300, less 10%
    assert json.loads(response["body"]) == {"order_id": "order-5f0c1d2e", "total_cents": 15570}
    assert line["has_promo"] is True
    assert (line["db_query_ms"], line["latency_ms"]) == (300.0, 380.0)
    assert line["dependency"] == "payments-api"
    assert (line["upstream_latency_ms"], line["upstream_timeout"], line["upstream_timeout_ms"]) == (80.0, 0, 2000.0)
    (upstream,) = [d for d in line["_aws"]["CloudWatchMetrics"] if d["Dimensions"] == [["dependency"]]]
    assert [metric["Name"] for metric in upstream["Metrics"]] == ["upstream_latency_ms", "upstream_timeout"]


def test_a_checkout_without_a_promo():
    response, line = serve(post("POST /checkout", {"items": [{"product_id": "p-1003", "quantity": 1}]}))
    assert json.loads(response["body"])["total_cents"] == 3200
    assert line["has_promo"] is False


def test_a_pool_timeout_is_a_500_that_names_the_pool():
    response, line = serve(get_product("p-1001"), FixedBackend(wait_s=1.5, timed_out=True, active=20, concurrent=47))
    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "internal error"}
    assert (line["level"], line["error"], line["error_type"]) == ("ERROR", 1, "TimeoutError")
    assert line["message"] == "TimeoutError: acquiring connection from pool (waited 1500 ms, pool_max_size=20)"
    assert (line["pool_wait_time_ms"], line["latency_ms"], line["concurrent_requests"]) == (1500.0, 1500.0, 47)
    assert "db_query_ms" not in line
    # The trace ends in the service's own code, and shows nothing of the simulation behind it.
    assert line["stack"][-1].startswith("checkout/db.py:")
    assert line["stack"][-1].endswith(" in wait_for_connection")
    assert not any("sim/" in frame for frame in line["stack"])


def test_a_payments_timeout_is_a_504_counted_as_upstream_not_as_an_error():
    response, line = serve(post("POST /checkout", {"items": ITEMS}), FixedBackend(hold_s=0.3, upstream_s=2.5))
    assert response["statusCode"] == 504
    assert (line["level"], line["error"], line["error_type"]) == ("ERROR", 0, "UpstreamTimeout")
    assert line["message"] == "UpstreamTimeout: payments-api did not respond within 2000 ms"
    assert (line["upstream_timeout"], line["upstream_latency_ms"], line["latency_ms"]) == (1, 2000.0, 2300.0)
    assert "stack" not in line


def test_an_unexpected_error_is_a_500_with_a_short_stack():
    response, line = serve(get_product("p-1001"), BrokenBackend())
    assert response["statusCode"] == 500
    assert (line["error"], line["error_type"], line["error_message"]) == (1, "RuntimeError", "ledger exploded")
    assert 0 < len(line["stack"]) <= 10


def test_an_unknown_route_is_a_404():
    response, line = serve({"routeKey": "DELETE /orders"})
    assert response["statusCode"] == 404
    assert line["error"] == 0


def test_cold_start_is_logged():
    _, line = serve({"routeKey": "GET /health"}, cold_start=True)
    assert line["cold_start"] is True


def test_the_package_version_is_the_service_version():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"] == VERSION
