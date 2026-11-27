"""sentinelops_core.trace against the page's contract (web/src/api/contract.ts): the same events, kinds, and fields.

The contract's TraceEvent is a union of one-line object types; each line's
kind and field names must be a model's, and each model's a line's. seq and at
are the read function's (the item's order and its at), not the event's.
"""

import re
from pathlib import Path
from typing import get_args

from sentinelops_core import trace

CONTRACT = (Path(__file__).resolve().parents[2] / "web" / "src" / "api" / "contract.ts").read_text(encoding="utf-8")


def contract_events() -> dict[str, set[str]]:
    union = CONTRACT[CONTRACT.index("export type TraceEvent") : CONTRACT.index("export type HaltReason")]
    found = {}
    for line in re.findall(r"^\s*\| \{ (kind: \"\w+\".*?) \}", union, re.M):
        kind = re.match(r'kind: "(\w+)"', line)[1]  # type: ignore[index]
        found[kind] = set(re.findall(r"(\w+): ", line)) - {"kind"}
    return found


def model_events() -> dict[str, set[str]]:
    models = get_args(get_args(trace.Event)[0])
    return {m.model_fields["kind"].default: set(m.model_fields) - {"kind"} for m in models}


def test_every_event_is_the_contracts_and_every_contract_event_is_one() -> None:
    contract = contract_events()

    assert len(contract) == 16
    assert model_events() == contract


def test_the_shared_names_and_caps_are_the_contracts() -> None:
    def union(name: str) -> set[str]:
        return set(re.findall(r'"(\w+)"', re.search(rf"export type {name} = ([^;]+);", CONTRACT)[1]))  # type: ignore[index]

    assert set(get_args(trace.AlarmKey)) == union("AlarmKey")
    assert set(get_args(trace.ToolName)) == union("ToolName")
    assert set(get_args(trace.HaltReason)) == union("HaltReason")
    assert f"export const TEXT_CHARS = {trace.TEXT_CHARS:_};" in CONTRACT
    assert trace.INCIDENT_ID.fullmatch("checkout-api-20261126T183624Z")
