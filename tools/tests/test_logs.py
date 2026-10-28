import json
from datetime import UTC, datetime

from conftest import Stubbed

from sentinelops_tools.logs import (
    MAX_CALLS,
    MAX_LINE_CHARS,
    MAX_RESULT_CHARS,
    filter_pattern,
    recent_lines,
)

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def body(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_the_newest_errors_come_back_whole_and_oldest_first(recorded) -> None:
    with recorded("logs_bad_deploy_errors") as (env, _):
        text = recent_lines(env)

    lines = body(text)
    assert len(lines) == 20
    assert text.startswith("20 checkout-api error lines, oldest first, newest 2 s old.")
    assert "Only the newest 20 of 25 found are shown." in text
    assert [line["timestamp"] for line in lines] == sorted(line["timestamp"] for line in lines)
    failure = lines[-1]
    assert failure["error_type"] == "TypeError"
    assert failure["version"] == "1.43.0"
    assert failure["stack"][-1] == "checkout/pricing.py:15 in apply_promo"
    assert "_aws" not in failure, "the EMF block is the metrics' copy, not the agent's"


def test_a_route_with_braces_filters_as_written(recorded) -> None:
    # Checked live: the filter matches GET /products/{id} with the braces as they are.
    with recorded("logs_bad_deploy_products") as (env, fixture):
        text = recent_lines(env, minutes=2, errors_only=False, route="GET /products/{id}", limit=3)

    assert fixture["calls"][0]["params"]["filterPattern"] == (
        '{ $.service = "checkout-api" && $.route = "GET /products/{id}" }'
    )
    lines = body(text)
    assert [line["route"] for line in lines] == ["GET /products/{id}"] * 3
    assert {line["status"] for line in lines} == {200}


def test_no_errors_searches_back_through_the_whole_window(recorded) -> None:
    # The pool fault at its first alarm: no failures yet. The search widens from
    # the newest 30 s back to the start of the 10 minutes, in 5 calls.
    with recorded("logs_pool_errors") as (env, fixture):
        text = recent_lines(env)

    windows = [(c["params"]["startTime"], c["params"]["endTime"]) for c in fixture["calls"]]
    widths = [end - start + 1 for start, end in windows]
    # Both ends are inclusive, so the last window reaches exactly 10 minutes back.
    assert widths == [30_000, 60_000, 120_000, 240_000, 150_001]
    assert windows[-1][0] == windows[0][1] - 600_000
    assert all(later[0] == earlier[1] + 1 for earlier, later in zip(windows[1:], windows))
    assert text.startswith("No checkout-api error lines from 18:46:46 to 18:56:46 UTC.")


def test_filter_patterns_name_only_fixed_values() -> None:
    assert filter_pattern("checkout-api", True, None) == '{ $.service = "checkout-api" && $.error = 1 }'
    assert filter_pattern("checkout-api", False, "POST /checkout") == (
        '{ $.service = "checkout-api" && $.route = "POST /checkout" }'
    )


def test_an_unknown_route_is_refused_without_a_call() -> None:
    stubbed = Stubbed(NOW)
    with stubbed:
        text = recent_lines(stubbed.env, route='GET /x" || $.level = "INFO')

    assert text.startswith("Unknown route")


def line(ms: int, n: int, size: int = 0) -> dict:
    record = {"timestamp": "t", "service": "checkout-api", "n": n, "pad": "x" * size, "_aws": {}}
    return {"eventId": f"e{n:04d}", "timestamp": ms, "message": json.dumps(record)}


def expect_window(stubbed: Stubbed, start: int, end: int, events: list[dict], token: str | None = None,
                  after: str | None = None, pattern: str = '{ $.service = "checkout-api" && $.error = 1 }'
                  ) -> None:
    params = {
        "logGroupName": "/aws/lambda/sentinelops-checkout-api",
        "startTime": start,
        "endTime": end,
        "filterPattern": pattern,
    }
    if after:
        params["nextToken"] = after
    response = {"events": events, **({"nextToken": token} if token else {})}
    stubbed.expect("logs", "filter_log_events", params, response)


def test_long_lines_are_cut_and_the_result_stays_under_its_cap() -> None:
    stubbed = Stubbed(NOW)
    events = [line(NOW_MS - 1000 * i, i, size=5_000) for i in range(25, 0, -1)]
    expect_window(stubbed, NOW_MS - 29_999, NOW_MS, events)
    with stubbed:
        text = recent_lines(stubbed.env, limit=25)

    rendered = [ln for ln in text.splitlines() if ln.startswith("{")]
    assert all(len(ln) <= MAX_LINE_CHARS + len(" ...[cut]") for ln in rendered)
    assert all(ln.endswith(" ...[cut]") for ln in rendered)
    assert len(text) <= MAX_RESULT_CHARS + 1_000
    assert len(rendered) == MAX_RESULT_CHARS // (MAX_LINE_CHARS + len(" ...[cut]") + 1)
    assert "lines were left out to stay under 40,000 characters" in text
    assert '"n":1,' in rendered[-1], "the newest line is never the one dropped"


def test_the_limit_is_capped_at_200_lines() -> None:
    stubbed = Stubbed(NOW)
    events = [line(NOW_MS - 10 * i, i) for i in range(250, 0, -1)]
    expect_window(stubbed, NOW_MS - 29_999, NOW_MS, events)
    with stubbed:
        text = recent_lines(stubbed.env, limit=500)

    assert len(body(text)) == 200
    assert "At most 200 lines per call." in text


def test_a_search_that_runs_out_of_calls_says_newer_lines_may_be_missing() -> None:
    stubbed = Stubbed(NOW)
    expect_window(stubbed, NOW_MS - 29_999, NOW_MS, [line(NOW_MS - 29_000, 1)], token="t1")
    for i in range(1, MAX_CALLS):
        expect_window(stubbed, NOW_MS - 29_999, NOW_MS, [], token=f"t{i + 1}", after=f"t{i}")
    with stubbed:
        text = recent_lines(stubbed.env)

    assert "ran out of calls partway through a window" in text


def test_an_aws_error_reaches_the_model_as_its_code_only() -> None:
    stubbed = Stubbed(NOW)
    stubbed.stubbers["logs"].add_client_error(
        "filter_log_events",
        service_error_code="ThrottlingException",
        service_message="Rate exceeded for arn:aws:logs:us-east-1:123456789012:log-group:x",
    )
    with stubbed:
        text = recent_lines(stubbed.env)

    assert text == "Could not read the logs: AWS returned ThrottlingException."
