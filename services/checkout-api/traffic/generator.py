"""The traffic generator: checkout-api's steady baseline.

EventBridge Scheduler invokes it once a minute, with retries off. Each run
sends mix.REQUESTS_PER_MINUTE requests from the mix, spread evenly over
SPREAD_S with a little jitter, so one run ends about as the next begins. Every
route requires IAM auth, so each request is signed with the function's role.
A failed request is counted, never raised: failures are what faults show.
"""

import json
import random
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from typing import Any

from traffic import mix

SPREAD_S = 50.0
JITTER_S = 1.0
TIMEOUT_S = 10.0  # checkout-api's own function timeout

Send = Callable[[mix.Call], str]  # sends one request; returns its status, or why there was none


def run(
    send: Send,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    rng: random.Random,
    count: int = mix.REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """Send `count` requests from the mix, evenly spread, and count the outcomes."""
    started = clock()
    outcomes: Counter[str] = Counter()
    for n in range(count):
        due = started + n * SPREAD_S / count + rng.uniform(0.0, JITTER_S)
        sleep(max(0.0, due - clock()))
        outcomes[send(mix.sample(rng))] += 1
    return {
        "requests": count,
        "outcomes": dict(sorted(outcomes.items())),
        "elapsed_s": round(clock() - started, 1),
    }


def signed_sender(base_url: str, region: str) -> Send:
    """Sends calls to checkout-api with SigV4, as the function's own role."""
    from botocore.auth import SigV4Auth  # botocore comes with the Lambda runtime
    from botocore.awsrequest import AWSRequest
    from botocore.session import get_session

    credentials = get_session().get_credentials()

    def send(call: mix.Call) -> str:
        data = None if call.body is None else json.dumps(call.body).encode("utf-8")
        headers = {} if data is None else {"content-type": "application/json"}
        request = AWSRequest(method=call.method, url=base_url + call.path, data=data, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(request)
        signed = urllib.request.Request(
            request.url, data=data, headers=dict(request.headers.items()), method=call.method
        )
        return deliver(signed)

    return send


def deliver(request: urllib.request.Request, timeout_s: float = TIMEOUT_S) -> str:
    """The response's status code, or the name of the error if there was no response."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return str(response.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except OSError as exc:  # DNS, connection, or timeout: no response at all
        return type(exc).__name__
