"""Lambda entry point for checkout-api.

Everything is built at import, during the init phase, so the DynamoDB clients'
setup never shows up in a request's latency_ms.
"""

import functools
import os
import random
import time
from typing import Any

from checkout.app import App
from checkout.config import Config
from sim import releases
from sim.backend import Backend
from sim.flags import FlagReader
from sim.pool import DynamoLedger

_flags = FlagReader.from_env(os.environ, Config.from_env(os.environ), time.time)


def _settings() -> Config:
    """This request's settings, and the release they name, deployed in this process."""
    settings = _flags()
    releases.deploy(settings.version)
    return settings


app = App(
    settings=_settings,
    # Wall clock, not monotonic: the ledger compares times across instances.
    backend=Backend(DynamoLedger.from_env(os.environ, time.time), random.Random()),
    clock=time.time,
    sleep=time.sleep,
    emit=functools.partial(print, flush=True),
)
_cold_start = True


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _cold_start
    cold_start, _cold_start = _cold_start, False
    return app.handle(event, request_id=context.aws_request_id, cold_start=cold_start)
