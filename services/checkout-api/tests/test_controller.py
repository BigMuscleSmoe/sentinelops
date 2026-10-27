"""The fault controller: what a break writes, what clear moves, and what status says."""

import importlib
import random
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from checkout import VERSION
from checkout.config import Config
from controller import faults
from controller.actions import FLAG_KEY, FaultController, timestamp
from sim import releases
from sim.flags import parse
from sim.pool_sim import VirtualClock

NOW = 1_800_000_000.25
EXPIRES_AT = 1_800_000_601  # the next whole second, plus 600
FLAGS = "sentinelops-checkout-flags"
HISTORY = "sentinelops-deploy-history"
REPO = Path(__file__).resolve().parents[3]


class Cancelled(Exception):
    """A TransactionCanceledException, as botocore raises it."""

    def __init__(self, *codes: str) -> None:
        super().__init__("Transaction cancelled")
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": code} for code in codes],
        }


class StubDynamo:
    """The flags table's item, and every transaction written. A Put of a flag
    stores it, and a Delete of one removes it."""

    def __init__(self) -> None:
        self.flag: dict | None = None
        self.transactions: list[list[dict]] = []
        self.refuse: Exception | None = None

    def get_item(self, **kwargs):
        assert (kwargs["TableName"], kwargs["Key"], kwargs["ConsistentRead"]) == (FLAGS, FLAG_KEY, True)
        return {"Item": self.flag} if self.flag else {}

    def transact_write_items(self, TransactItems):
        if self.refuse:
            raise self.refuse
        self.transactions.append(TransactItems)
        for write in TransactItems:
            if "Put" in write and write["Put"]["TableName"] == FLAGS:
                self.flag = write["Put"]["Item"]
            if "Delete" in write and write["Delete"]["TableName"] == FLAGS:
                self.flag = None


@pytest.fixture
def clock():
    return VirtualClock(NOW)


@pytest.fixture
def dynamo():
    return StubDynamo()


@pytest.fixture
def controller(dynamo, clock):
    return FaultController(dynamo, FLAGS, HISTORY, clock, random.Random(4))


def epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def puts(transaction: list[dict]) -> list[dict]:
    return [write["Put"] for write in transaction]


def test_a_break_writes_the_flag_cause_decoy_and_rollback_in_one_transaction(controller, dynamo):
    result = controller.handle({"action": "break", "mode": "db_pool_exhaustion"})

    assert result == {"ok": True, "active": True, "mode": "db_pool_exhaustion", "expires_at": timestamp(EXPIRES_AT)}
    (transaction,) = dynamo.transactions
    flag, cause, decoy, rollback = puts(transaction)
    assert flag["TableName"] == FLAGS
    assert flag["Item"]["overrides"] == {"M": {"POOL_MAX_SIZE": {"N": "2"}}}
    assert (flag["Item"]["set_at"], flag["Item"]["expires_at"]) == ({"N": f"{NOW:.3f}"}, {"N": str(EXPIRES_AT)})
    assert flag["ConditionExpression"] == "attribute_not_exists(#flag) OR #expires_at <= :now"

    assert {put["TableName"] for put in (cause, decoy, rollback)} == {HISTORY}
    assert {put["ConditionExpression"] for put in (cause, decoy, rollback)} == {"attribute_not_exists(#ts)"}
    assert cause["Item"]["ts"] == {"S": timestamp(NOW)}
    assert cause["Item"]["changes"] == {"L": [{"M": {"key": {"S": "POOL_MAX_SIZE"}, "old": {"S": "20"}, "new": {"S": "2"}}}]}
    assert 3600 <= NOW - epoch(decoy["Item"]["ts"]["S"]) <= 6 * 3600
    assert decoy["Item"]["kind"] == {"S": "config"}
    assert rollback["Item"]["ts"] == flag["Item"]["rollback_ts"] == {"S": timestamp(EXPIRES_AT)}
    assert rollback["Item"]["changes"]["L"][0]["M"]["new"] == {"S": "20"}


def test_a_bad_deploy_is_recorded_as_a_deploy_of_1_43_0(controller, dynamo):
    controller.handle({"action": "break", "mode": "bad_deploy"})
    flag, cause, _, rollback = puts(dynamo.transactions[0])
    assert flag["Item"]["overrides"] == {"M": {"release": {"S": "1.43.0"}}}
    assert (cause["Item"]["kind"], cause["Item"]["version"]) == ({"S": "deploy"}, {"S": "1.43.0"})
    assert cause["Item"]["commit"] == {"S": faults.BAD_RELEASE_COMMIT}
    assert (rollback["Item"]["version"], rollback["Item"]["commit"]) == ({"S": VERSION}, {"S": faults.RELEASE_COMMIT})


@pytest.mark.parametrize(
    "mode, expected",
    [("db_pool_exhaustion", {"pool_max_size": 2}), ("bad_deploy", {"version": releases.BAD_RELEASE})],
)
def test_checkout_api_reads_each_flag_as_its_fault(controller, dynamo, mode, expected):
    controller.handle({"action": "break", "mode": mode})
    flag = parse(dynamo.flag)
    assert flag is not None and dict(flag.changes) == expected and flag.expires_at == EXPIRES_AT


def test_a_break_while_a_fault_is_active_is_refused(controller, dynamo):
    controller.handle({"action": "break", "mode": "bad_deploy"})
    dynamo.refuse = Cancelled("ConditionalCheckFailed", "None", "None", "None")
    result = controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    assert (result["ok"], result["error"], result["mode"]) == (False, "a fault is already active", "bad_deploy")


def test_any_other_failure_is_raised(controller, dynamo):
    dynamo.refuse = Cancelled("None", "ConditionalCheckFailed", "None", "None")  # a history key was taken
    with pytest.raises(Cancelled):
        controller.handle({"action": "break", "mode": "db_pool_exhaustion"})


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"action": "break", "mode": "downstream_timeout"}, "unknown mode 'downstream_timeout'"),
        ({"action": "break"}, "unknown mode None"),
        ({"action": "explode"}, "unknown action 'explode'"),
        ({}, "unknown action None"),
    ],
)
def test_a_bad_payload_is_refused_without_writing(controller, dynamo, payload, error):
    result = controller.handle(payload)
    assert result["ok"] is False and result["error"].startswith(error)
    assert dynamo.transactions == []


def test_clear_deletes_the_flag_and_moves_the_rollback_to_now(controller, dynamo, clock):
    controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    clock.sleep(120)
    assert controller.handle({"action": "clear"}) == {"ok": True, "cleared": "db_pool_exhaustion"}

    delete_flag, delete_rollback, put_rollback = dynamo.transactions[1]
    assert delete_flag["Delete"]["Key"] == FLAG_KEY
    assert delete_flag["Delete"]["ExpressionAttributeValues"] == {":set_at": {"N": f"{NOW:.3f}"}}
    assert delete_rollback["Delete"] == {
        "TableName": HISTORY,
        "Key": {"service": {"S": "checkout-api"}, "ts": {"S": timestamp(EXPIRES_AT)}},
    }
    assert put_rollback["Put"]["Item"]["ts"] == {"S": timestamp(NOW + 120)}
    assert put_rollback["Put"]["Item"]["message"] == {"S": faults.FAULTS["db_pool_exhaustion"].rollback.message}
    assert dynamo.flag is None


def test_clearing_an_expired_flag_only_deletes_it(controller, dynamo, clock):
    controller.handle({"action": "break", "mode": "bad_deploy"})
    clock.now = EXPIRES_AT
    assert controller.handle({"action": "clear"}) == {"ok": True, "cleared": None}
    (delete_flag,) = dynamo.transactions[1]
    assert delete_flag["Delete"]["TableName"] == FLAGS


def test_clear_with_no_flag_writes_nothing(controller, dynamo):
    assert controller.handle({"action": "clear"}) == {"ok": True, "cleared": None}
    assert dynamo.transactions == []


def test_status_shows_the_fault_until_it_expires(controller, clock):
    assert controller.handle({"action": "status"}) == {"ok": True, "active": False}
    controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    clock.sleep(100)
    assert controller.handle({"action": "status"}) == {
        "ok": True,
        "active": True,
        "mode": "db_pool_exhaustion",
        "set_at": timestamp(NOW),
        "expires_at": timestamp(EXPIRES_AT),
        "remaining_s": 501,
    }
    clock.now = EXPIRES_AT
    assert controller.handle({"action": "status"}) == {"ok": True, "active": False}


def test_timestamps_sort_as_text_in_time_order():
    times = [NOW - 7200.5, NOW, NOW + 0.001, NOW + 600]
    assert sorted(timestamp(t) for t in times) == [timestamp(t) for t in times]
    assert timestamp(NOW) == "2027-01-15T08:00:00.250Z"


def test_the_copies_match_checkout_api_and_the_seeded_history():
    assert faults.RELEASE == VERSION
    assert faults.BAD_RELEASE == releases.BAD_RELEASE
    assert (faults.POOL_MAX_SIZE, faults.SMALL_POOL_MAX_SIZE) == (Config().pool_max_size, 2)
    seeded = (REPO / "infra" / "checkout_data.tf").read_text(encoding="utf-8")
    assert f'"{faults.RELEASE_COMMIT}"' in seeded and f'"{faults.RELEASE}"' in seeded


def test_the_lambda_entry_point_wires_the_controller(monkeypatch, dynamo, capsys):
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service: dynamo
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setenv("FLAGS_TABLE", FLAGS)
    monkeypatch.setenv("DEPLOY_HISTORY_TABLE", HISTORY)
    monkeypatch.delitem(sys.modules, "controller.handler", raising=False)
    module = importlib.import_module("controller.handler")
    try:
        assert module.handler({"action": "status"}, None) == {"ok": True, "active": False}
        assert '"action": "status"' in capsys.readouterr().out
    finally:
        sys.modules.pop("controller.handler", None)
