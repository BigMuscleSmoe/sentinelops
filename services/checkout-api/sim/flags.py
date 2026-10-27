"""The fault flag: overrides for checkout-api's settings while a fault lasts.

The fault controller writes at most one item, flag = "active", to the flags
table, with `overrides` ({"POOL_MAX_SIZE": 2} or {"release": "1.43.0"}) and
`expires_at` in epoch seconds. Each instance reads it at most every 2 s, and
applies it until expires_at, whatever DynamoDB's TTL has done with the item.
When reads fail, the last item read still counts for 60 s from when it was
read; after that the defaults do.

The warnings here name no override, value, or file, so a fault never shows in
the service's logs.
"""

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from checkout.config import Config
from sim import releases

logger = logging.getLogger("checkout.config")

KEY = {"flag": {"S": "active"}}
CACHE_S = 2.0
STALE_S = 60.0


@dataclass(frozen=True)
class Flag:
    expires_at: float
    changes: Mapping[str, Any] = field(default_factory=dict)  # Config fields to replace


def parse(item: Mapping[str, Any]) -> Flag | None:
    """The flag in a DynamoDB item, or None if it can't be read at all."""
    try:
        expires_at = float(item["expires_at"]["N"])
        overrides = item["overrides"]["M"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(overrides, dict):
        return None
    changes: dict[str, Any] = {}
    for name, value in overrides.items():
        if name == "POOL_MAX_SIZE":
            size = _positive_int(value)
            if size is not None:
                changes["pool_max_size"] = size
        elif name == "release" and isinstance(value, dict) and value.get("S") in releases.RELEASES:
            changes["version"] = value["S"]
    if len(changes) < len(overrides):
        logger.warning("ignoring %d of %d config overrides", len(overrides) - len(changes), len(overrides))
    return Flag(expires_at, changes)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value["N"])
    except (KeyError, TypeError, ValueError):
        return None
    return number if number > 0 else None


class FlagReader:
    """The settings for each request: the defaults, with the flag's overrides while it lasts."""

    def __init__(
        self,
        table: str,
        client: Any,
        defaults: Config,
        clock: Callable[[], float],
        cache_s: float = CACHE_S,
        stale_s: float = STALE_S,
    ) -> None:
        self._table = table
        self._client = client
        self._defaults = defaults
        self._clock = clock
        self._cache_s = cache_s
        self._stale_s = stale_s
        self._flag: Flag | None = None  # from the last read that worked
        self._read_at = -math.inf  # when that read was
        self._tried_at = -math.inf  # the last read, whether it worked or not

    @classmethod
    def from_env(cls, env: Mapping[str, str], defaults: Config, clock: Callable[[], float]) -> "FlagReader":
        import boto3  # provided by the Lambda runtime; tests pass a stub client
        from botocore.config import Config as BotoConfig

        client = boto3.client(
            "dynamodb",
            config=BotoConfig(
                connect_timeout=1, read_timeout=1, retries={"max_attempts": 2, "mode": "standard"}
            ),
        )
        return cls(env["FLAGS_TABLE"], client, defaults, clock)

    def __call__(self) -> Config:
        now = self._clock()
        if now - self._tried_at >= self._cache_s:
            self._refresh(now)
        flag = self._flag
        if flag is None or now - self._read_at > self._stale_s or now >= flag.expires_at:
            return self._defaults
        return replace(self._defaults, **flag.changes)

    def _refresh(self, now: float) -> None:
        self._tried_at = now
        try:
            response = self._client.get_item(TableName=self._table, Key=KEY, ConsistentRead=True)
        except Exception as exc:
            if now - self._read_at > self._stale_s:
                logger.warning("config refresh failed (%s); using the defaults", type(exc).__name__)
            else:
                logger.warning(
                    "config refresh failed (%s); keeping the settings read %.0f s ago",
                    type(exc).__name__,
                    now - self._read_at,
                )
            return
        item = response.get("Item")
        self._flag = parse(item) if item else None
        if item and self._flag is None:
            logger.warning("ignoring a config override item it can't read")
        self._read_at = now
