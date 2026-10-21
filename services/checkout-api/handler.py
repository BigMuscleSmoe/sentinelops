"""Lambda entry point for checkout-api.

Everything is built at import, during the init phase, so the DynamoDB client's
setup never shows up in a request's latency_ms.
"""

import functools
import os
import random
import time
from typing import Any

from checkout.app import App
from checkout.config import Config
from sim.backend import Backend
from sim.pool import DynamoLedger

_config = Config.from_env(os.environ)
app = App(
    settings=lambda: _config,
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
