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

# payments-api while healthy: fast, with a tail well inside the 2 s client timeout.
PAYMENTS_MEDIAN_S = 0.080
PAYMENTS_SIGMA = 0.5


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def sample_hold(route: str, rng: random.Random) -> float:
    return lognormal(rng, HOLD_MEDIAN_S[route], HOLD_SIGMA)


def sample_payments_latency(rng: random.Random) -> float:
    return lognormal(rng, PAYMENTS_MEDIAN_S, PAYMENTS_SIGMA)
