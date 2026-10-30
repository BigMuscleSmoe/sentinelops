"""The store's DynamoDB calls, checked by botocore's Stubber against the real API model."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from store import CAP_PER_HOUR, LOCK_TTL_S, TRIAGE_TRIES, Store, TriageConflict
from triage import ALARM, Change, ServiceState, Verdict, classify

TABLE = "sentinelops-incidents"
SERVICE = "checkout-api"
ERRORS = "sentinelops-checkout-errors"
POOL = "sentinelops-checkout-pool-wait"
FIRED = datetime(2026, 10, 27, 19, 5, 57, 26000, tzinfo=UTC)
CHANGE = Change(ERRORS, ALARM, FIRED)
NOW = 1_793_392_075.5
STATE_KEY = {"pk": {"S": SERVICE}, "sk": {"S": "state"}}
LOCK_KEY = {"pk": {"S": "global"}, "sk": {"S": "lock"}}
CAP_KEY = {"pk": {"S": "global"}, "sk": {"S": "hourly-cap"}}


@pytest.fixture
def dynamo() -> Iterator[tuple[Store, Stubber]]:
    client = boto3.client(
        "dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    with Stubber(client) as stubber:
        yield Store(client, TABLE), stubber
        stubber.assert_no_pending_responses()


def state_item(state: ServiceState, version: int) -> dict:
    return {**STATE_KEY, "state": {"S": json.dumps(state.to_dict(), separators=(",", ":"))}, "version": {"N": str(version)}}


def expect_state_read(stubber: Stubber, state: ServiceState | None, version: int = 0) -> None:
    stubber.add_response(
        "get_item",
        {} if state is None else {"Item": state_item(state, version)},
        {"TableName": TABLE, "Key": STATE_KEY, "ConsistentRead": True},
    )


def cancelled(*reasons: str) -> dict:
    return {
        "service_error_code": "TransactionCanceledException",
        "service_message": "Transaction cancelled",
        "modeled_fields": {"CancellationReasons": [{"Code": r} for r in reasons]},
    }


def test_the_first_alarm_opens_an_incident_in_one_transaction(dynamo) -> None:
    store, stubber = dynamo
    expected = classify(ServiceState(), CHANGE, SERVICE)
    expect_state_read(stubber, None)
    stubber.add_response(
        "transact_write_items",
        {},
        {
            "TransactItems": [
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": state_item(expected.state, 1),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": {
                            "pk": {"S": SERVICE},
                            "sk": {"S": f"change#2026-10-27T19:05:57.026Z#{ERRORS}"},
                            "alarm": {"S": ERRORS},
                            "state": {"S": "ALARM"},
                            "at": {"S": "2026-10-27T19:05:57.026Z"},
                            "verdict": {"S": "new"},
                            "incident": {"S": "checkout-api-20261027T190557Z"},
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": {
                            "pk": {"S": SERVICE},
                            "sk": {"S": "incident#checkout-api-20261027T190557Z"},
                            "incident": {"S": "checkout-api-20261027T190557Z"},
                            "opened_at": {"S": "2026-10-27T19:05:57.026Z"},
                            "opened_by": {"S": ERRORS},
                            "status": {"S": "opened"},
                        },
                    }
                },
            ]
        },
    )

    result = store.triage(SERVICE, CHANGE)

    assert result.verdict is Verdict.NEW
    assert result.state == expected.state


def test_a_change_that_loses_the_race_is_judged_again_against_the_winner(dynamo) -> None:
    store, stubber = dynamo
    # The pool-wait alarm opened an incident between this change's read and its write.
    pool = classify(ServiceState(), Change(POOL, ALARM, FIRED.replace(microsecond=0)), SERVICE)
    expect_state_read(stubber, ServiceState(), 3)
    stubber.add_client_error("transact_write_items", **cancelled("ConditionalCheckFailed", "None", "None"))
    expect_state_read(stubber, pool.state, 4)
    retried = classify(pool.state, CHANGE, SERVICE)
    stubber.add_response(
        "transact_write_items",
        {},
        {
            "TransactItems": [
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": state_item(retried.state, 5),
                        "ConditionExpression": "#version = :version",
                        "ExpressionAttributeNames": {"#version": "version"},
                        "ExpressionAttributeValues": {":version": {"N": "4"}},
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": {
                            "pk": {"S": SERVICE},
                            "sk": {"S": f"change#2026-10-27T19:05:57.026Z#{ERRORS}"},
                            "alarm": {"S": ERRORS},
                            "state": {"S": "ALARM"},
                            "at": {"S": "2026-10-27T19:05:57.026Z"},
                            "verdict": {"S": "duplicate"},
                            "incident": {"S": "checkout-api-20261027T190557Z"},
                        },
                    }
                },
            ]
        },
    )

    result = store.triage(SERVICE, CHANGE)

    assert result.verdict is Verdict.DUPLICATE
    assert result.incident.alarmed == (POOL, ERRORS)


def test_a_change_already_recorded_writes_nothing(dynamo) -> None:
    store, stubber = dynamo
    expect_state_read(stubber, classify(ServiceState(), CHANGE, SERVICE).state, 1)

    assert store.triage(SERVICE, CHANGE).verdict is Verdict.SEEN


def test_a_transaction_cancelled_for_another_reason_raises(dynamo) -> None:
    store, stubber = dynamo
    expect_state_read(stubber, None)
    stubber.add_client_error("transact_write_items", **cancelled("None", "ThrottlingError", "None"))

    with pytest.raises(ClientError):
        store.triage(SERVICE, CHANGE)


def test_triage_gives_up_when_the_state_never_holds_still(dynamo) -> None:
    store, stubber = dynamo
    for _ in range(TRIAGE_TRIES):
        expect_state_read(stubber, None)
        stubber.add_client_error("transact_write_items", **cancelled("ConditionalCheckFailed", "None", "None"))

    with pytest.raises(TriageConflict):
        store.triage(SERVICE, CHANGE)


def lock_item(holder: str, now: int = int(NOW)) -> dict:
    return {**LOCK_KEY, "holder": {"S": holder}, "acquired_at": {"N": str(now)}, "expires_at": {"N": str(now + LOCK_TTL_S)}}


def test_the_lock_is_taken_only_if_free_or_expired(dynamo) -> None:
    store, stubber = dynamo
    expected = {
        "TableName": TABLE,
        "Item": lock_item("session-a"),
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        "ConditionExpression": "attribute_not_exists(pk) OR expires_at < :now",
        "ExpressionAttributeValues": {":now": {"N": "1793392075"}},
    }
    stubber.add_response("put_item", {}, expected)
    stubber.add_client_error(
        "put_item",
        "ConditionalCheckFailedException",
        modeled_fields={"Item": lock_item("session-b", int(NOW) - 30)},
        expected_params=expected,
    )

    assert store.lock("session-a", NOW) is True
    assert store.lock("session-a", NOW) is False


def test_a_retried_lock_that_finds_its_own_first_try_holds_it(dynamo) -> None:
    store, stubber = dynamo
    stubber.add_client_error(
        "put_item", "ConditionalCheckFailedException", modeled_fields={"Item": lock_item("session-a")}
    )

    assert store.lock("session-a", NOW) is True


def test_only_the_holder_releases_the_lock(dynamo) -> None:
    store, stubber = dynamo
    expected = {
        "TableName": TABLE,
        "Key": LOCK_KEY,
        "ConditionExpression": "holder = :holder",
        "ExpressionAttributeValues": {":holder": {"S": "session-a"}},
    }
    stubber.add_response("delete_item", {}, expected)
    stubber.add_client_error("delete_item", "ConditionalCheckFailedException", expected_params=expected)

    assert store.unlock("session-a") is True
    assert store.unlock("session-a") is False


def cap_item(starts: list[int], version: int) -> dict:
    return {**CAP_KEY, "starts": {"L": [{"N": str(s)} for s in starts]}, "version": {"N": str(version)}}


def expect_cap_read(stubber: Stubber, item: dict | None) -> None:
    stubber.add_response(
        "get_item", {} if item is None else {"Item": item}, {"TableName": TABLE, "Key": CAP_KEY, "ConsistentRead": True}
    )


def test_the_cap_counts_only_the_last_hour(dynamo) -> None:
    store, stubber = dynamo
    now = int(NOW)
    recent = [now - 60 * i for i in range(1, CAP_PER_HOUR)]  # 19 in the last hour
    old = [now - 3600, now - 7200]  # an hour ago exactly, and two
    expect_cap_read(stubber, cap_item(recent + old, 7))
    stubber.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": cap_item([*recent, now], 8),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            "ConditionExpression": "#version = :version",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {":version": {"N": "7"}},
        },
    )

    assert store.take_cap_slot(NOW) is True


def test_the_cap_refuses_the_21st_in_an_hour(dynamo) -> None:
    store, stubber = dynamo
    expect_cap_read(stubber, cap_item([int(NOW) - 60 * i for i in range(CAP_PER_HOUR)], 20))

    assert store.take_cap_slot(NOW) is False


def test_the_first_investigation_creates_the_cap_and_a_lost_race_refuses(dynamo) -> None:
    store, stubber = dynamo
    expected = {
        "TableName": TABLE,
        "Item": cap_item([int(NOW)], 1),
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        "ConditionExpression": "attribute_not_exists(pk)",
    }
    expect_cap_read(stubber, None)
    stubber.add_response("put_item", {}, expected)
    expect_cap_read(stubber, None)
    stubber.add_client_error(
        "put_item",
        "ConditionalCheckFailedException",
        modeled_fields={"Item": cap_item([int(NOW) - 5], 1)},
        expected_params=expected,
    )

    assert store.take_cap_slot(NOW) is True
    assert store.take_cap_slot(NOW) is False


def test_a_retried_cap_write_that_finds_its_own_first_try_counts(dynamo) -> None:
    store, stubber = dynamo
    expect_cap_read(stubber, None)
    stubber.add_client_error(
        "put_item", "ConditionalCheckFailedException", modeled_fields={"Item": cap_item([int(NOW)], 1)}
    )

    assert store.take_cap_slot(NOW) is True


def test_incident_fields_are_stored_with_their_types(dynamo) -> None:
    store, stubber = dynamo
    stubber.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": {"pk": {"S": SERVICE}, "sk": {"S": "incident#checkout-api-20261027T190557Z"}},
            "UpdateExpression": "SET #f0 = :v0, #f1 = :v1, #f2 = :v2, #f3 = :v3, #f4 = :v4, #f5 = :v5",
            "ExpressionAttributeNames": {
                "#f0": "status",
                "#f1": "input_tokens",
                "#f2": "cost_usd",
                "#f3": "tool_calls",
                "#f4": "record_gz",
                "#f5": "flag",
            },
            "ExpressionAttributeValues": {
                ":v0": {"S": "done"},
                ":v1": {"N": "36783"},
                ":v2": {"N": "0.00001"},
                ":v3": {"L": [{"S": "get_metrics"}, {"S": "get_recent_logs"}]},
                ":v4": {"B": b"\x1f\x8b"},
                ":v5": {"BOOL": True},
            },
        },
    )

    store.update_incident(
        SERVICE,
        "checkout-api-20261027T190557Z",
        {
            "status": "done",
            "input_tokens": 36783,
            "cost_usd": 1e-05,
            "tool_calls": ["get_metrics", "get_recent_logs"],
            "record_gz": b"\x1f\x8b",
            "flag": True,
        },
    )


def test_an_incident_field_it_cant_store_is_refused() -> None:
    store = Store(None, TABLE)

    with pytest.raises(TypeError):
        store.update_incident(SERVICE, "x", {"nested": {"a": 1}})
