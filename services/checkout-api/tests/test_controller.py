"""The fault controller: what a break writes and starts, what clear moves, and what status says."""

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


class Bursts:
    """Starts no traffic: records each burst, as how many transactions had landed by then."""

    def __init__(self, dynamo: StubDynamo) -> None:
        self.dynamo = dynamo
        self.started: list[int] = []
        self.refuse: Exception | None = None

    def __call__(self) -> None:
        if self.refuse:
            raise self.refuse
        self.started.append(len(self.dynamo.transactions))


class AccessDeniedException(Exception):
    """Named like the error botocore raises when the role may not invoke the burst."""


@pytest.fixture
def clock():
    return VirtualClock(NOW)


@pytest.fixture
def dynamo():
    return StubDynamo()


@pytest.fixture
def bursts(dynamo):
    return Bursts(dynamo)


@pytest.fixture
def controller(dynamo, clock, bursts):
    return FaultController(dynamo, FLAGS, HISTORY, clock, random.Random(4), bursts)


def epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def puts(transaction: list[dict]) -> list[dict]:
    return [write["Put"] for write in transaction]


def test_a_break_writes_the_flag_cause_decoy_and_rollback_in_one_transaction(controller, dynamo):
    result = controller.handle({"action": "break", "mode": "db_pool_exhaustion"})

    assert result == {
        "ok": True,
        "active": True,
        "mode": "db_pool_exhaustion",
        "expires_at": timestamp(EXPIRES_AT),
        "burst": True,
    }
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


@pytest.mark.parametrize("mode", ["db_pool_exhaustion", "bad_deploy"])
def test_every_break_starts_the_burst_once_its_transaction_has_landed(controller, bursts, mode):
    controller.handle({"action": "break", "mode": mode})
    assert bursts.started == [1]


def test_a_break_while_a_fault_is_active_is_refused_and_starts_no_burst(controller, dynamo, bursts):
    controller.handle({"action": "break", "mode": "bad_deploy"})
    dynamo.refuse = Cancelled("ConditionalCheckFailed", "None", "None", "None")
    result = controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    assert (result["ok"], result["error"], result["mode"]) == (False, "a fault is already active", "bad_deploy")
    assert bursts.started == [1]  # only the first break's


def test_any_other_failure_is_raised(controller, dynamo, bursts):
    dynamo.refuse = Cancelled("None", "ConditionalCheckFailed", "None", "None")  # a history key was taken
    with pytest.raises(Cancelled):
        controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    assert bursts.started == []


def test_a_burst_that_fails_to_start_is_reported_and_the_fault_stays(controller, bursts):
    bursts.refuse = AccessDeniedException("not authorized to invoke")
    result = controller.handle({"action": "break", "mode": "db_pool_exhaustion"})

    assert (result["ok"], result["active"], result["burst"], result["mode"]) == (
        False,
        True,
        False,
        "db_pool_exhaustion",
    )
    assert "burst didn't start (AccessDeniedException)" in result["error"]
    assert controller.handle({"action": "status"})["active"] is True


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"action": "break", "mode": "downstream_timeout"}, "unknown mode 'downstream_timeout'"),
        ({"action": "break"}, "unknown mode None"),
        ({"action": "explode"}, "unknown action 'explode'"),
        ({}, "unknown action None"),
    ],
)
def test_a_bad_payload_is_refused_without_writing(controller, dynamo, bursts, payload, error):
    result = controller.handle(payload)
    assert result["ok"] is False and result["error"].startswith(error)
    assert dynamo.transactions == []
    assert bursts.started == []


def test_clear_deletes_the_flag_and_moves_the_rollback_to_now(controller, dynamo, clock, bursts):
    controller.handle({"action": "break", "mode": "db_pool_exhaustion"})
    clock.sleep(120)
    assert controller.handle({"action": "clear"}) == {"ok": True, "cleared": "db_pool_exhaustion"}
    assert bursts.started == [1]  # clear starts nothing, and leaves a running burst to run out

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


class StubLambda:
    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


def test_the_lambda_entry_point_wires_the_controller_and_the_burst(monkeypatch, dynamo, capsys):
    lambda_client = StubLambda()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service: {"dynamodb": dynamo, "lambda": lambda_client}[service]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setenv("FLAGS_TABLE", FLAGS)
    monkeypatch.setenv("DEPLOY_HISTORY_TABLE", HISTORY)
    monkeypatch.setenv("BURST_FUNCTION", "sentinelops-traffic-burst")
    monkeypatch.delitem(sys.modules, "controller.handler", raising=False)
    module = importlib.import_module("controller.handler")
    try:
        assert module.handler({"action": "status"}, None) == {"ok": True, "active": False}
        assert '"action": "status"' in capsys.readouterr().out
        assert module.handler({"action": "break", "mode": "bad_deploy"}, None)["burst"] is True
        # Asynchronous, so the controller returns at once, and with nothing that names the fault.
        assert lambda_client.invocations == [
            {"FunctionName": "sentinelops-traffic-burst", "InvocationType": "Event", "Payload": b"{}"}
        ]
    finally:
        sys.modules.pop("controller.handler", None)
