"""The flow's reads and its one write on the trigger's table, against botocore's own DynamoDB model."""

import boto3
import pytest
from botocore.stub import Stubber

from sentinelops_remediation.incidents import Answered, Incidents

TABLE = "sentinelops-incidents"
KEY = {"pk": {"S": "checkout-api"}, "sk": {"S": "incident#checkout-api-20261125T043000Z"}}
READ_NAMES = {"#a0": "incident", "#a1": "opened_at", "#a2": "opened_by", "#a3": "status", "#a4": "hypothesis"}
ITEM = {
    "incident": {"S": "checkout-api-20261125T043000Z"},
    "opened_at": {"S": "2026-11-25T04:30:00.123Z"},
    "opened_by": {"S": "sentinelops-checkout-errors"},
    "status": {"S": "done"},
    "hypothesis": {"S": '{"root_cause": "x"}'},
}


def stubbed() -> tuple[Incidents, Stubber]:
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    return Incidents(client, TABLE), Stubber(client)


def expected_get() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "ConsistentRead": True,
        "ProjectionExpression": "#a0, #a1, #a2, #a3, #a4",
        "ExpressionAttributeNames": READ_NAMES,
    }


def test_an_answered_incident_is_read_consistently_and_only_its_five_fields() -> None:
    incidents, stub = stubbed()
    stub.add_response("get_item", {"Item": ITEM}, expected_get())
    with stub:
        found = incidents.get("checkout-api", "checkout-api-20261125T043000Z")
    assert found == Answered(
        "checkout-api-20261125T043000Z", "2026-11-25T04:30:00.123Z", "sentinelops-checkout-errors", "done", '{"root_cause": "x"}'
    )
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "item",
    [None, {k: v for k, v in ITEM.items() if k != "opened_by"}, {**ITEM, "status": {"N": "1"}}],
    ids=["missing", "a-field-missing", "a-field-not-a-string"],
)
def test_an_incident_not_as_the_trigger_writes_it_is_none(item: dict | None) -> None:
    incidents, stub = stubbed()
    stub.add_response("get_item", {} if item is None else {"Item": item}, expected_get())
    with stub:
        assert incidents.get("checkout-api", "checkout-api-20261125T043000Z") is None


def test_a_still_running_incident_has_no_hypothesis() -> None:
    incidents, stub = stubbed()
    running = {k: v for k, v in ITEM.items() if k != "hypothesis"} | {"status": {"S": "investigating"}}
    stub.add_response("get_item", {"Item": running}, expected_get())
    with stub:
        found = incidents.get("checkout-api", "checkout-api-20261125T043000Z")
    assert found is not None and (found.status, found.hypothesis) == ("investigating", None)


def test_the_result_is_one_whole_map_written_only_on_an_incident_that_exists() -> None:
    incidents, stub = stubbed()
    stub.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": KEY,
            "UpdateExpression": "SET #r = :r",
            "ConditionExpression": "attribute_exists(sk)",
            "ExpressionAttributeNames": {"#r": "remediation"},
            "ExpressionAttributeValues": {
                ":r": {
                    "M": {
                        "status": {"S": "ready"},
                        "pr_number": {"N": "7"},
                        "pushed": {"BOOL": True},
                        "conclusion": {"NULL": True},
                    }
                }
            },
        },
    )
    with stub:
        incidents.record(
            "checkout-api", "checkout-api-20261125T043000Z", {"status": "ready", "pr_number": 7, "pushed": True, "conclusion": None}
        )
    stub.assert_no_pending_responses()


def test_a_value_dynamodb_cant_hold_as_given_is_refused() -> None:
    incidents, _ = stubbed()
    with pytest.raises(TypeError):
        incidents.record("checkout-api", "checkout-api-20261125T043000Z", {"pull": {"number": 7}})
