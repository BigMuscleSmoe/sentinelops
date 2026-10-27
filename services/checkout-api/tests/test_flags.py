"""The flag reader: the flag's overrides while it lasts, read at most every 2 s,
and the defaults once reads have failed for a minute."""

import pytest

from checkout.config import Config
from sim.flags import FlagReader
from sim.pool_sim import VirtualClock

START = 1_800_000_000.0
TABLE = "sentinelops-checkout-flags"
DEFAULTS = Config()
SMALL_POOL = {"POOL_MAX_SIZE": {"N": "2"}}


def flag_item(overrides, expires_at=START + 600):
    return {
        "flag": {"S": "active"},
        "mode": {"S": "unread"},
        "overrides": {"M": overrides},
        "set_at": {"N": str(START)},
        "expires_at": {"N": str(expires_at)},
    }


class StubTable:
    def __init__(self, item=None) -> None:
        self.item = item
        self.failing = False
        self.reads: list[dict] = []

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        if self.failing:
            raise ConnectionError("could not reach the endpoint")
        return {"Item": self.item} if self.item else {}


@pytest.fixture
def clock():
    return VirtualClock(START)


def reader(table, clock):
    return FlagReader(TABLE, table, DEFAULTS, clock)


def test_no_flag_means_the_defaults(clock):
    assert reader(StubTable(), clock)() == DEFAULTS


def test_the_read_is_one_consistent_get_by_key(clock):
    table = StubTable()
    reader(table, clock)()
    assert table.reads == [{"TableName": TABLE, "Key": {"flag": {"S": "active"}}, "ConsistentRead": True}]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        (SMALL_POOL, Config(pool_max_size=2)),
        ({"release": {"S": "1.43.0"}}, Config(version="1.43.0")),
    ],
    ids=["pool size", "release"],
)
def test_an_override_replaces_its_setting(clock, overrides, expected):
    assert reader(StubTable(flag_item(overrides)), clock)() == expected


def test_the_flag_is_read_at_most_every_2_s(clock):
    table = StubTable()
    flags = reader(table, clock)
    flags()
    clock.sleep(1.9)
    flags()
    assert len(table.reads) == 1
    clock.sleep(0.1)
    flags()
    assert len(table.reads) == 2


def test_a_new_flag_applies_within_2_s(clock):
    table = StubTable()
    flags = reader(table, clock)
    assert flags() == DEFAULTS
    table.item = flag_item(SMALL_POOL)
    clock.sleep(2.0)
    assert flags().pool_max_size == 2


def test_the_flag_stops_counting_at_expires_at_though_the_item_is_still_there(clock):
    flags = reader(StubTable(flag_item(SMALL_POOL, expires_at=START + 600)), clock)
    clock.now = START + 599.9
    assert flags().pool_max_size == 2
    clock.now = START + 600
    assert flags() == DEFAULTS


def test_when_reads_fail_the_last_flag_counts_for_60_s_then_the_defaults(clock, caplog):
    table = StubTable(flag_item(SMALL_POOL))
    flags = reader(table, clock)
    assert flags().pool_max_size == 2
    table.failing = True
    clock.sleep(30)
    assert flags().pool_max_size == 2
    clock.sleep(30)
    assert flags().pool_max_size == 2
    clock.sleep(2)
    assert flags() == DEFAULTS
    assert "keeping the settings read 30 s ago" in caplog.text
    assert "config refresh failed (ConnectionError); using the defaults" in caplog.text
    table.failing = False
    clock.sleep(2)
    assert flags().pool_max_size == 2


def test_a_failed_first_read_means_the_defaults(clock, caplog):
    table = StubTable(flag_item(SMALL_POOL))
    table.failing = True
    assert reader(table, clock)() == DEFAULTS
    assert "using the defaults" in caplog.text


def test_overrides_it_cannot_apply_are_skipped_and_the_warning_names_none(clock, caplog):
    item = flag_item(
        {
            "POOL_MAX_SIZE": {"N": "0"},
            "release": {"S": "9.9.9"},
            "payments_api": {"S": "degraded"},
            "PAYMENTS_TIMEOUT_MS": {"S": "fast"},
        }
    )
    assert reader(StubTable(item), clock)() == DEFAULTS
    assert "ignoring 4 of 4 config overrides" in caplog.text
    for detail in ("POOL", "release", "payments", "PAYMENTS", "9.9.9", "degraded", "fast"):
        assert detail not in caplog.text


@pytest.mark.parametrize("missing", ["expires_at", "overrides"])
def test_an_item_it_cannot_read_counts_as_no_flag(clock, caplog, missing):
    item = flag_item(SMALL_POOL)
    del item[missing]
    assert reader(StubTable(item), clock)() == DEFAULTS
    assert "ignoring a config override item it can't read" in caplog.text
