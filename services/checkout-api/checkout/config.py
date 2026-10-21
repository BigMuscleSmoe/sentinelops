"""Service configuration, read from the environment at cold start."""

from collections.abc import Mapping
from dataclasses import dataclass

from checkout import VERSION


@dataclass(frozen=True)
class Config:
    version: str = VERSION
    pool_max_size: int = 20
    pool_acquire_timeout_s: float = 1.5
    payments_timeout_s: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        return cls(
            pool_max_size=int(env.get("POOL_MAX_SIZE", "20")),
            pool_acquire_timeout_s=int(env.get("POOL_ACQUIRE_TIMEOUT_MS", "1500")) / 1000,
            payments_timeout_s=int(env.get("PAYMENTS_TIMEOUT_MS", "2000")) / 1000,
        )
