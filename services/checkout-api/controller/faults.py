"""What each fault does to checkout-api, and what deploy history says about it.

A fault is the overrides checkout-api applies while the flag lasts, the change
deploy history records when it starts, and the rollback it records when it
ends. Every break also records one decoy: a harmless change from 1 to 6 hours
earlier, so the newest change in the history isn't always the cause.

The versions, sizes, and commit here copy checkout-api's own, so this package
ships alone. tests/test_controller.py holds them to the originals.
"""

from dataclasses import dataclass
from typing import Any

SERVICE = "checkout-api"
RELEASE = "1.42.0"  # what checkout-api runs when healthy
RELEASE_COMMIT = "3f8a2d1"  # as infra/checkout_data.tf seeds it
BAD_RELEASE = "1.43.0"
BAD_RELEASE_COMMIT = "9c41e7b"
POOL_MAX_SIZE = 20
SMALL_POOL_MAX_SIZE = 2
DURATION_S = 600  # how long a fault lasts unless it's cleared


@dataclass(frozen=True)
class Change:
    """A deploy-history record, less its service and timestamp."""

    kind: str  # "deploy" or "config"
    version: str  # the release running once the change is made
    commit: str
    author: str
    message: str
    changes: tuple[tuple[str, str, str], ...]  # (key, old, new)


@dataclass(frozen=True)
class Fault:
    overrides: dict[str, Any]  # DynamoDB attribute values, as sim/flags.py reads them
    cause: Change
    rollback: Change


FAULTS = {
    "db_pool_exhaustion": Fault(
        overrides={"POOL_MAX_SIZE": {"N": str(SMALL_POOL_MAX_SIZE)}},
        cause=Change(
            "config", RELEASE, RELEASE_COMMIT, "maya",
            "Size the checkout DB pool to the new replica's connection limit",
            (("POOL_MAX_SIZE", str(POOL_MAX_SIZE), str(SMALL_POOL_MAX_SIZE)),),
        ),
        rollback=Change(
            "config", RELEASE, RELEASE_COMMIT, "oncall", "Revert the checkout DB pool size",
            (("POOL_MAX_SIZE", str(SMALL_POOL_MAX_SIZE), str(POOL_MAX_SIZE)),),
        ),
    ),
    "bad_deploy": Fault(
        overrides={"release": {"S": BAD_RELEASE}},
        cause=Change(
            "deploy", BAD_RELEASE, BAD_RELEASE_COMMIT, "tomas", "Simplify promo pricing",
            (("version", RELEASE, BAD_RELEASE),),
        ),
        rollback=Change(
            "deploy", RELEASE, RELEASE_COMMIT, "oncall", f"Roll back to {RELEASE}",
            (("version", BAD_RELEASE, RELEASE),),
        ),
    ),
}

DECOYS = (
    Change(
        "config", RELEASE, RELEASE_COMMIT, "ken", "Keep abandoned carts for two days",
        (("CART_TTL_HOURS", "24", "48"),),
    ),
    Change(
        "config", RELEASE, RELEASE_COMMIT, "priya", "Cache product lookups for a minute",
        (("PRODUCT_CACHE_TTL_S", "30", "60"),),
    ),
    Change(
        "config", RELEASE, RELEASE_COMMIT, "tomas", "Sample a quarter of debug logs",
        (("LOG_SAMPLE_RATE", "0.1", "0.25"),),
    ),
    Change(
        "config", RELEASE, RELEASE_COMMIT, "maya", "Turn on product recommendations",
        (("RECOMMENDATIONS_ENABLED", "false", "true"),),
    ),
)
