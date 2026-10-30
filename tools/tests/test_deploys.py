import json
from datetime import UTC, datetime

import pytest
from conftest import Stubbed

from sentinelops_tools.deploys import history

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)


def records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_history_runs_newest_first_and_stops_at_now(recorded) -> None:
    with recorded("history_bad_deploy") as (env, fixture):
        text = history(env)

    query = fixture["calls"][0]["params"]
    assert query["KeyConditionExpression"] == "#service = :service AND #ts <= :now"
    assert query["ExpressionAttributeValues"][":now"] == {"S": "2026-10-27T19:05:57.026Z"}
    assert query["ScanIndexForward"] is False
    assert query["Limit"] == 11  # one past the 10 it shows

    found = records(text)
    assert [r["ts"] for r in found] == sorted((r["ts"] for r in found), reverse=True)
    assert found[0] == {
        "ts": "2026-10-27T19:05:22.912Z",
        "kind": "deploy",
        "version": "1.43.0",
        "commit": "9c41e7b",
        "author": "tomas",
        "message": "Simplify promo pricing",
        "changes": [{"key": "version", "old": "1.42.0", "new": "1.43.0"}],
    }
    # The rollback this break scheduled, dated later, is not in view.
    assert all(r["ts"] <= "2026-10-27T19:05:57.026Z" for r in found)
    assert "Older records exist" in text


def test_a_config_change_reads_key_old_new(recorded) -> None:
    with recorded("history_pool") as (env, _):
        text = history(env, limit=4)

    newest = records(text)[0]
    assert newest["kind"] == "config"
    assert list(newest["changes"][0]) == ["key", "old", "new"]
    assert newest["changes"][0] == {"key": "POOL_MAX_SIZE", "old": "20", "new": "2"}


def query_params(limit: int) -> dict:
    names = ["ts", "kind", "version", "commit", "author", "message", "changes"]
    return {
        "TableName": "sentinelops-deploy-history",
        "KeyConditionExpression": "#service = :service AND #ts <= :now",
        "ProjectionExpression": ", ".join(f"#{n}" for n in names),
        "ExpressionAttributeNames": {"#service": "service", **{f"#{n}": n for n in names}},
        "ExpressionAttributeValues": {
            ":service": {"S": "checkout-api"},
            ":now": {"S": "2026-10-28T04:00:00.000Z"},
        },
        "ScanIndexForward": False,
        "Limit": limit,
    }


def test_the_limit_is_capped_at_20() -> None:
    stubbed = Stubbed(NOW)
    stubbed.expect("dynamodb", "query", query_params(21), {"Items": []})
    with stubbed:
        assert history(stubbed.env, limit=100).startswith("No deploys or config changes")


@pytest.mark.parametrize(("found", "older"), [(2, False), (3, True)])
def test_older_records_are_mentioned_only_when_one_more_came_back(found: int, older: bool) -> None:
    # DynamoDB answers any page of exactly Limit items with a LastEvaluatedKey,
    # older records or not, so the tool asks for one more than it shows.
    items = [{"ts": {"S": f"2026-10-2{7 - n}T12:00:00.000Z"}, "kind": {"S": "deploy"}} for n in range(found)]
    response: dict = {"Items": items, "Count": found, "ScannedCount": found}
    if found == 3:
        response["LastEvaluatedKey"] = {"service": {"S": "checkout-api"}, "ts": items[-1]["ts"]}
    stubbed = Stubbed(NOW)
    stubbed.expect("dynamodb", "query", query_params(3), response)
    with stubbed:
        text = history(stubbed.env, limit=2)

    assert [r["ts"] for r in records(text)] == ["2026-10-27T12:00:00.000Z", "2026-10-26T12:00:00.000Z"]
    assert ("Older records exist" in text) is older


def test_an_aws_error_reaches_the_model_as_its_code_only() -> None:
    stubbed = Stubbed(NOW)
    stubbed.stubbers["dynamodb"].add_client_error(
        "query",
        service_error_code="ResourceNotFoundException",
        service_message="Requested resource not found: arn:aws:dynamodb:us-east-1:123456789012:table/x",
    )
    with stubbed:
        text = history(stubbed.env)

    assert text == "Could not read the deploy history: AWS returned ResourceNotFoundException."
