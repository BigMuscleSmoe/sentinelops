"""Lambda entry points for the traffic generator and the burst."""

import json
import os
import random
import time
from typing import Any

from traffic import burst
from traffic.generator import run, signed_sender

_send = signed_sender(os.environ["CHECKOUT_API_URL"], os.environ["AWS_REGION"])
_rng = random.Random()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    summary = run(_send, time.time, time.sleep, _rng)
    print(json.dumps({"message": "traffic sent", **summary}), flush=True)
    return summary


def burst_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """The same burst for every break: the event is ignored."""
    summary = burst.run(_send, burst.schedule(_rng), time.time, time.sleep, _rng)
    print(json.dumps({"message": "burst sent", **summary}), flush=True)
    return summary
