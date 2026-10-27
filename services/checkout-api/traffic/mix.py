"""The traffic mix: what the generator sends checkout-api, and what the harness
in sim/pool_sim.py replays. One definition, so the alarm timings the harness
predicts are for the traffic that really arrives."""

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REQUESTS_PER_MINUTE = 12
ROUTE_MIX = {"GET /products/{id}": 0.5, "POST /cart/items": 0.3, "POST /checkout": 0.2}
UNKNOWN_PRODUCT_SHARE = 0.02  # a stale link: a 404
PROMO_SHARE = 0.3
# checkout-api's catalog and promo codes, copied so this package ships on its
# own. tests/test_traffic.py holds them to the service's.
PRODUCT_IDS = tuple(f"p-{n}" for n in range(1001, 1013))
UNKNOWN_PRODUCT_ID = "p-9999"
PROMO_CODES = ("TRAIL10", "SUMMIT20")


@dataclass(frozen=True)
class Call:
    """One request: the route it exercises, and what goes on the wire."""

    route: str  # the HTTP API route key, e.g. "GET /products/{id}"
    path: str
    path_parameters: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, Any] | None = None

    @property
    def method(self) -> str:
        return self.route.split(" ", 1)[0]


def sample(rng: random.Random) -> Call:
    """One request from the mix."""
    route = rng.choices(list(ROUTE_MIX), weights=list(ROUTE_MIX.values()))[0]
    if route == "GET /products/{id}":
        product_id = UNKNOWN_PRODUCT_ID if rng.random() < UNKNOWN_PRODUCT_SHARE else rng.choice(PRODUCT_IDS)
        return Call(route, f"/products/{product_id}", {"id": product_id})
    if route == "POST /cart/items":
        return Call(route, "/cart/items", body={"product_id": rng.choice(PRODUCT_IDS), "quantity": rng.randint(1, 3)})
    chosen = rng.sample(PRODUCT_IDS, rng.randint(1, 3))
    body: dict[str, Any] = {"items": [{"product_id": p, "quantity": rng.randint(1, 2)} for p in chosen]}
    if rng.random() < PROMO_SHARE:
        body["promo_code"] = rng.choice(PROMO_CODES)
    return Call(route, "/checkout", body=body)
