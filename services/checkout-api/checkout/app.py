"""Request handling: four routes, and one log line per request."""

import base64
import functools
import json
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checkout import db, payments, telemetry
from checkout.catalog import PRODUCTS, Product
from checkout.config import Config
from checkout.errors import BadRequest, HttpError, NotFound
from checkout.pricing import price_cart
from sim.backend import Backend, Timeline

SERVICE = "checkout-api"
HEALTH = "GET /health"
PROMOS = {
    "TRAIL10": {"percent_off": 10},
    "SUMMIT20": {"percent_off": 20},
}
MAX_QUANTITY = 10
MAX_LINE_ITEMS = 20
MAX_STACK_FRAMES = 10
CODE_ROOT = Path(__file__).resolve().parent.parent

Response = tuple[int, dict[str, Any]]


@dataclass
class Request:
    route: str
    request_id: str
    event: Mapping[str, Any]
    settings: Config
    fields: dict[str, Any] = field(default_factory=dict)  # added to the log line


class App:
    def __init__(
        self,
        settings: Callable[[], Config],
        backend: Backend,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        emit: Callable[[str], None],
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._clock = clock
        self._sleep = sleep
        self._emit = emit
        self._routes: dict[str, Callable[[Request], Response]] = {
            HEALTH: self._health,
            "GET /products/{id}": self._get_product,
            "POST /cart/items": self._add_to_cart,
            "POST /checkout": self._checkout,
        }

    def handle(self, event: Mapping[str, Any], request_id: str, cold_start: bool) -> dict[str, Any]:
        """Serve one HTTP API request (payload format 2.0)."""
        started = self._clock()
        request = Request(event.get("routeKey", ""), request_id, event, self._settings())
        detail = ""
        try:
            route = self._routes.get(request.route)
            if route is None:
                raise NotFound(f"no route for {request.route}")
            status, body = route(request)
        except payments.UpstreamTimeout as exc:
            status, body = 504, {"error": "payment provider timed out"}
            request.fields.update(error_type=type(exc).__name__, error_message=str(exc))
        except HttpError as exc:
            status, body = exc.status, {"error": str(exc)}
            detail = str(exc)
        except Exception as exc:
            status, body = 500, {"error": "internal error"}
            request.fields.update(
                error_type=type(exc).__name__, error_message=str(exc), stack=_stack(exc)
            )
        finished = self._clock()

        if "error_type" in request.fields:
            message = f"{request.fields['error_type']}: {request.fields['error_message']}"
        else:
            message = f"{request.route} {status}" + (f": {detail}" if detail else "")
        fields = {
            "level": "ERROR" if status >= 500 else "INFO",
            "message": message,
            "service": SERVICE,
            "version": request.settings.version,
            "request_id": request_id,
            "route": request.route,
            "status": status,
            "latency_ms": _ms(finished - started),
            "error": 1 if status == 500 else 0,  # this service's own failures; 504s count as upstream_timeout
            **request.fields,
            "cold_start": cold_start,
        }
        self._emit(telemetry.log_line(fields, finished, metrics=request.route != HEALTH))
        return {
            "statusCode": status,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(body),
        }

    def _health(self, request: Request) -> Response:
        return 200, {"status": "ok", "service": SERVICE, "version": request.settings.version}

    def _get_product(self, request: Request) -> Response:
        product_id = (request.event.get("pathParameters") or {}).get("id", "")
        self._query(request)
        product = PRODUCTS.get(product_id)
        if product is None:
            raise NotFound(f"product {product_id} not found")
        return 200, {"id": product.id, "name": product.name, "price_cents": product.price_cents}

    def _add_to_cart(self, request: Request) -> Response:
        body = _json_body(request.event)
        product_id = _string(body, "product_id")
        quantity = _quantity(body.get("quantity"))
        cart_id = _string(body, "cart_id") if "cart_id" in body else f"cart-{request.request_id[:8]}"
        self._query(request)
        if product_id not in PRODUCTS:
            raise NotFound(f"product {product_id} not found")
        return 201, {"cart_id": cart_id, "product_id": product_id, "quantity": quantity}

    def _checkout(self, request: Request) -> Response:
        body = _json_body(request.event)
        items = _line_items(body.get("items"))
        promo = _promo(body.get("promo_code"))
        request.fields["has_promo"] = promo is not None
        total_cents = price_cart(items, promo)
        timeline = self._query(request)  # writes the order; the connection is back before the charge

        timeout_s = request.settings.payments_timeout_s
        request.fields.update(
            dependency=payments.DEPENDENCY, upstream_timeout_ms=_ms(timeout_s), upstream_timeout=0
        )
        called = self._clock()
        try:
            payments.charge(timeline, timeout_s, self._sleep_until)
        except payments.UpstreamTimeout:
            request.fields["upstream_timeout"] = 1
            raise
        finally:
            request.fields["upstream_latency_ms"] = _ms(self._clock() - called)
        return 200, {"order_id": f"order-{request.request_id[:8]}", "total_cents": total_cents}

    def _query(self, request: Request) -> Timeline:
        """Take a connection from the shared pool, run the route's query, and give it back."""
        settings = request.settings
        timeline = self._backend.connect(request.route, settings)
        request.fields.update(
            pool_max_size=settings.pool_max_size,
            pool_active_connections=timeline.active_connections,
            pool_wait_time_ms=_ms(timeline.wait_s),
            concurrent_requests=timeline.concurrent_requests,
        )
        db.wait_for_connection(timeline, settings.pool_max_size, self._sleep_until)
        db.run_query(timeline, self._sleep_until)
        request.fields["db_query_ms"] = _ms(timeline.hold_s)
        return timeline

    def _sleep_until(self, deadline: float) -> None:
        self._sleep(max(0.0, deadline - self._clock()))


def _json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        body = json.loads(raw)
    except ValueError as exc:  # also covers bad base64 and bad UTF-8
        raise BadRequest("body must be JSON") from exc
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


def _string(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise BadRequest(f"{name} must be a string of 1 to 64 characters")
    return value


def _quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_QUANTITY:
        raise BadRequest(f"quantity must be a whole number from 1 to {MAX_QUANTITY}")
    return value


def _line_items(raw: Any) -> list[tuple[Product, int]]:
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_LINE_ITEMS:
        raise BadRequest(f"items must be a list of 1 to {MAX_LINE_ITEMS} line items")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise BadRequest("each line item must be an object")
        product = PRODUCTS.get(_string(entry, "product_id"))
        if product is None:
            raise BadRequest(f"unknown product {entry['product_id']}")
        items.append((product, _quantity(entry.get("quantity"))))
    return items


def _promo(code: Any) -> Mapping[str, Any] | None:
    if code is None:
        return None
    promo = PROMOS.get(code) if isinstance(code, str) else None
    if promo is None:
        raise BadRequest("unknown promo code")
    return promo


def _stack(exc: BaseException) -> list[str]:
    """The innermost frames, as path:line in function."""
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_STACK_FRAMES:]
    return [f"{_source_path(frame.filename)}:{frame.lineno} in {frame.name}" for frame in frames]


@functools.lru_cache(maxsize=64)
def _source_path(filename: str) -> str:
    try:
        return Path(filename).resolve().relative_to(CODE_ROOT).as_posix()
    except ValueError:  # outside the service, e.g. the standard library
        return filename


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)
