"""How long things take. Lognormal, like most latency."""

import math
import random

# Median time a route's query holds its database connection.
HOLD_MEDIAN_S = {
    "GET /products/{id}": 0.050,
    "POST /cart/items": 0.070,
    "POST /checkout": 0.300,
}
HOLD_SIGMA = 0.35

# payments-api's modes, as (median s, sigma). Healthy: fast, with a tail well
# inside the 2 s client timeout. Degraded, the downstream_timeout fault: a median
# of 700 ms, and 4.0% of calls past 2 s, which checkout-api answers with a 504.
HEALTHY = "healthy"
DEGRADED = "degraded"
PAYMENTS = {HEALTHY: (0.080, 0.5), DEGRADED: (0.700, 0.6)}


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def sample_hold(route: str, rng: random.Random) -> float:
    return lognormal(rng, HOLD_MEDIAN_S[route], HOLD_SIGMA)


def sample_payments_latency(rng: random.Random, mode: str = HEALTHY) -> float:
    median, sigma = PAYMENTS[mode]
    return lognormal(rng, median, sigma)
