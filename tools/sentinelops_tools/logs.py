"""checkout-api's newest request log lines, whole.

Lines come back whole, never as picked fields: Logs Insights flattens arrays,
so a picked `stack` comes back empty without any error. FilterLogEvents
returns each event as written. It can still trail ingestion under load, so the
newest lines may be missing, and their absence proves nothing on its own.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sentinelops_tools.env import ToolEnv, aws_errors_as_text, clock

ROUTES = ("GET /products/{id}", "POST /cart/items", "POST /checkout")
MAX_LINES = 200  # CLAUDE.md: log payloads entering the model's context cap at 200 lines
MAX_LINE_CHARS = 2_000
MAX_RESULT_CHARS = 40_000
MAX_MINUTES = 180
FIRST_WINDOW_S = 30
MAX_CALLS = 20


@aws_errors_as_text("the logs")
def recent_lines(
    env: ToolEnv,
    minutes: int = 10,
    errors_only: bool = True,
    route: str | None = None,
    limit: int = 20,
) -> str:
    if route is not None and route not in ROUTES:
        return f"Unknown route {route!r}. The routes are: {', '.join(ROUTES)}."
    notes = []
    minutes = min(max(1, minutes), MAX_MINUTES)
    if limit > MAX_LINES:
        notes.append(f"At most {MAX_LINES} lines per call.")
    limit = min(max(1, limit), MAX_LINES)

    end_ms = _ms(env.now())
    floor_ms = end_ms - minutes * 60_000
    pattern = filter_pattern(env.service, errors_only, route)
    events, calls_left, high, complete = [], MAX_CALLS, end_ms, True
    width_ms = FIRST_WINDOW_S * 1000
    # Newest first: FilterLogEvents reads forward in time, so step back through
    # widening windows until there are enough lines or the window is used up.
    while complete and high > floor_ms and len(events) < limit and calls_left > 0:
        low = max(floor_ms, high - width_ms + 1)
        found, calls_left, complete = _filter(env, low, high, pattern, calls_left)
        events = found + events
        high, width_ms = low - 1, width_ms * 2
    searched_from_ms = high + 1

    shown = events[-limit:]
    rendered = [_render(e["message"]) for e in shown]
    while len(rendered) > 1 and sum(len(r) + 1 for r in rendered) > MAX_RESULT_CHARS:
        rendered.pop(0)
    if len(rendered) < len(shown):
        notes.append(
            f"The oldest {len(shown) - len(rendered)} lines were left out to stay under "
            f"{MAX_RESULT_CHARS:,} characters; narrow the route or the window to see them."
        )

    what = "error lines" if errors_only else "request lines"
    where = f" on {route}" if route else ""
    window = f"{clock(_dt(searched_from_ms))} to {clock(_dt(end_ms))} UTC"
    if not rendered:
        head = f"No checkout-api {what}{where} from {window}."
    else:
        age_s = (end_ms - shown[-1]["timestamp"]) / 1000
        head = (
            f"{len(rendered)} checkout-api {what}{where}, oldest first, newest {age_s:.0f} s old. "
            f"Searched {window}."
        )
        if len(events) > len(shown):
            head += f" Only the newest {len(shown)} of {len(events)} found are shown."
    if not complete:
        notes.append(
            "The search ran out of calls partway through a window, so newer matching lines "
            "may be missing; narrow the window or the route."
        )
    elif calls_left == 0 and high > floor_ms and len(events) < limit:
        notes.append("The search stopped before covering the whole window.")
    notes.append("Logs can trail the metrics by minutes under load; recent lines may be missing.")
    return "\n".join([head, *notes, *rendered])


def filter_pattern(service: str, errors_only: bool, route: str | None) -> str:
    """A JSON filter pattern. Every value in it comes from a fixed set, never from free text."""
    terms = [f'$.service = "{service}"']
    if errors_only:
        terms.append("$.error = 1")
    if route is not None:
        terms.append(f'$.route = "{route}"')
    return "{ " + " && ".join(terms) + " }"


def _filter(
    env: ToolEnv, low_ms: int, high_ms: int, pattern: str, calls_left: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """Every matching event in [low_ms, high_ms], oldest first, unless the calls run out."""
    request: dict[str, Any] = {
        "logGroupName": env.log_group,
        "startTime": low_ms,
        "endTime": high_ms,
        "filterPattern": pattern,
    }
    events: dict[str, dict[str, Any]] = {}
    complete = False
    while calls_left > 0:
        calls_left -= 1
        response = env.logs.filter_log_events(**request)
        for event in response.get("events", []):
            events[event["eventId"]] = event
        token = response.get("nextToken")
        if not token:
            complete = True
            break
        request["nextToken"] = token
    ordered = sorted(events.values(), key=lambda e: (e["timestamp"], e["eventId"]))
    return ordered, calls_left, complete


def _render(message: str) -> str:
    """The line as written, minus the EMF block the metrics came from."""
    try:
        record = json.loads(message)
    except ValueError:
        line = message.strip()
    else:
        if isinstance(record, dict):
            record.pop("_aws", None)
            line = json.dumps(record, separators=(",", ":"))
        else:
            line = message.strip()
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + " ...[cut]"
    return line


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)
