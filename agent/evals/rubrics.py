"""Each scenario's rubric, in code: is the diagnosis right, and does its evidence carry it?

Items, each scored on its own (BUILD_PLAN.md, Phase 3 design):
  names_culprit     root_cause names the change that caused it
  names_mechanism   root_cause says how that change produces the symptom
  blames_no_other   root_cause blames no other change: a suspect named in a
                    sentence that rules it out ("not", "separate", "already
                    reverted") is accounted for, not blamed
  location          affected_component is this service, not the upstream, and
                    so is fault_location where the answer has one
  pr_gate           Hypothesis.can_open_pr, read through the property, is what
                    the scenario expects
  runbook           runbook_cited is what the scenario expects: null, while
                    there's no runbook search
  evidence_change   an evidence item cites the culprit's own deploy record, by
                    a field that shows the change (its version, or the changed
                    key), and holds there
  evidence_failure  an evidence item that holds shows the mechanism: a logged
                    error naming it, or the pool's wait or use as a number
  evidence_symptom  an evidence item that holds gives the alarm's own metric as
                    a number, from a metrics table or the alarm message

The last three keep a Hypothesis that says less from scoring better. They read
the checker's verdicts and what the cited entries show (BUILD_PLAN.md, "The
grader redesign", step 4), never an item's words: an item that doesn't hold
carries nothing, and a loose one only what it got right. `diagnosis` is the
first four together; `confidently_wrong` is a wrong diagnosis that would still
pass the PR gate.

The first four match words, so they're brittle to phrasing: the hand audit
records its own verdict on each diagnosis, and the two are compared.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from checker.checks import ItemCheck, Verdict, field_of, is_number
from evals.scenarios import ERRORS, LATENCY, POOL_WAIT, Fault, Scenario
from models import FaultLocation, Hypothesis

# Arrows and dashes the model writes, as ASCII, so the rules can stay ASCII.
_ASCII = {0x2192: "->", 0x2013: "-", 0x2014: "-", 0x2212: "-", 0x2248: "~", 0x00D7: "x"}
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_RULED_OUT = re.compile(
    r"\bnot\b|n't\b|\bno longer\b|\brule[sd]? out\b|\bunrelated\b|\bseparate\b|\bresolved\b|\bearlier\b"
    r"|\balready\b|\brather than\b|\binstead of\b|\breverted\b|\bprior\b|\bprevious\b"
    r"|\bunchanged\b|\bhealthy\b"
    r"|\bstayed flat\b|\bunaffected\b|\bno changes?\b"
    # A state, not an adjective: "a steady climb" or "above normal" rules nothing out.
    r"|\b(?:is|are|was|were|stays|stay|stayed|remains|remain|remained|looks|look|looked) normal\b|\bnormal range\b"
    r"|\b(?:is|are|was|were|holds|hold|held|stays|stay|stayed|remains|remain|remained) steady\b",
    re.I,
)
_DECOY_KEYS = r"LOG_SAMPLE_RATE|CART_TTL|PRODUCT_CACHE_TTL|RECOMMENDATIONS_ENABLED"
_SHOWN = (Verdict.HOLDS, Verdict.LOOSE)  # what an item got right; a loose one's extra words aren't
_STATES_A_NUMBER = ("value", "range", "all")  # a count states how many, a first or last a time


@dataclass(frozen=True)
class FaultRules:
    culprit: re.Pattern[str]
    mechanism: tuple[re.Pattern[str], ...]  # every one must match
    wrong_suspects: re.Pattern[str]
    # The mechanism in the cited data: a logged value that names it (an error type
    # is itself the observation), or a column whose number shows it (a wait).
    failure_value: re.Pattern[str] | None
    failure_column: re.Pattern[str] | None


RULES = {
    Fault.BAD_DEPLOY: FaultRules(
        culprit=re.compile(r"1\.43\.0|9c41e7b|simplify promo pricing", re.I),
        mechanism=(
            re.compile(r"promo", re.I),
            re.compile(r"\bNone\b|NoneType|(?i:null|TypeError|subscript|dereferenc)"),
        ),
        wrong_suspects=re.compile(
            rf"POOL_MAX_SIZE|pool[_ ]max|pool size|pool[- ]exhaust|connection pool|{_DECOY_KEYS}", re.I
        ),
        failure_value=re.compile(r"TypeError|NoneType|apply_promo|pricing\.py"),
        failure_column=None,
    ),
    Fault.DB_POOL_EXHAUSTION: FaultRules(
        culprit=re.compile(
            r"POOL_MAX_SIZE|pool[_ ]max|max(imum)? pool|pool (max )?size|pool (limit|cap)|pool to 2\b"
            r"|20\s*(->|to)\s*2\b",
            re.I,
        ),
        mechanism=(
            re.compile(
                r"exhaust|saturat|wait|queue|contention|starv|ran out|run out|no (free|idle|available)"
                r"|all \w+ (of the )?(\w+ )?connections",
                re.I,
            ),
        ),
        wrong_suspects=re.compile(
            rf"1\.43\.0|9c41e7b|promo|TypeError|payments-api|slow quer|database (is |was )?slow|{_DECOY_KEYS}", re.I
        ),
        failure_value=None,
        failure_column=re.compile(r"^pool_(wait|active)"),
    ),
}
# Each alarm's own metric, as the metrics table heads it; the alarm message's datapoints are it too.
_SYMPTOM = {ERRORS: "err%", LATENCY: "p99_ms", POOL_WAIT: "pool_wait_p99_ms"}
# Every scenario so far is a fault inside this service, plain enough to name with confidence.
EXPECT_PR = True
EXPECT_RUNBOOK = None


@dataclass
class RubricResult:
    items: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def diagnosis(self) -> bool:
        return all(self.items[k] for k in ("names_culprit", "names_mechanism", "blames_no_other", "location"))

    @property
    def confidently_wrong(self) -> bool:
        return self.items["pr_gate_value"] and not self.diagnosis

    def passed(self) -> bool:
        return all(v for k, v in self.items.items() if k != "pr_gate_value")


def ascii_text(text: str) -> str:
    return text.translate(_ASCII)


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text.strip()) if s]


def blamed(text: str, suspects: re.Pattern[str]) -> list[str]:
    """The suspects named in a sentence that doesn't rule them out."""
    return [m.group() for s in sentences(text) if not _RULED_OUT.search(s) for m in suspects.finditer(s)]


def shown(check: ItemCheck) -> list[str]:
    """What an item's column holds at the entries it cites, as text; a list's elements joined."""
    out = []
    for entry in check.entries:
        value = field_of(entry, check.column)
        out.append(" ".join(map(str, value)) if isinstance(value, list) else str(value))
    return out


def shows_a_number(check: ItemCheck) -> bool:
    return check.kind in _STATES_A_NUMBER and any(is_number(field_of(e, check.column)) for e in check.entries)


def evidence_items(scenario: Scenario, checks: list[ItemCheck]) -> dict[str, bool]:
    """The three evidence items, from the checker's verdicts and the entries each item cites."""
    rules = RULES[scenario.fault]
    right = [c for c in checks if c.verdict in _SHOWN]
    culprit_minute = f"{scenario.fired_at:%Y-%m-%d}T{scenario.culprit_at}"

    def is_culprit(c: ItemCheck) -> bool:
        return any(e.time is not None and f"{e.time:%Y-%m-%dT%H:%M}" == culprit_minute for e in c.entries)

    change = any(
        c.verdict is Verdict.HOLDS and (c.source or "").startswith("d") and is_culprit(c)
        and any(rules.culprit.search(f"{c.column} {text}") for text in shown(c))
        for c in right
    )
    failure = any(
        (rules.failure_value is not None and any(rules.failure_value.search(text) for text in shown(c)))
        or (rules.failure_column is not None and bool(rules.failure_column.search(c.column or "")) and shows_a_number(c))
        for c in right
    )
    metric = _SYMPTOM[scenario.alarm]
    symptom = any(
        (c.source == "alarm" or ((c.source or "").startswith("m") and c.column == metric)) and shows_a_number(c)
        for c in right
    )
    return {"evidence_change": change, "evidence_failure": failure, "evidence_symptom": symptom}


def pr_gate(hypothesis: dict[str, Any]) -> bool:
    """Hypothesis.can_open_pr for the answer, read through the property."""
    return Hypothesis.model_validate(hypothesis).can_open_pr


def score(scenario: Scenario, hypothesis: dict[str, Any], checks: list[ItemCheck]) -> RubricResult:
    """The rubric for one answer, with the checker's checks of its evidence (`checker.checks`)."""
    rules = RULES[scenario.fault]
    result = RubricResult()
    cause = ascii_text(hypothesis.get("root_cause") or "")
    component = ascii_text(hypothesis.get("affected_component") or "")

    result.items["names_culprit"] = bool(rules.culprit.search(cause))
    result.items["names_mechanism"] = all(p.search(cause) for p in rules.mechanism)
    wrong = blamed(cause, rules.wrong_suspects)
    result.items["blames_no_other"] = not wrong
    if wrong:
        result.notes["blames_no_other"] = "blames " + ", ".join(sorted(set(wrong)))
    in_service = re.search(r"checkout|pool", component, re.I) is not None
    upstream = re.search(r"payments", component, re.I) is not None
    located = hypothesis.get("fault_location") == FaultLocation.THIS_SERVICE
    result.items["location"] = in_service and not upstream and located

    gate = pr_gate(hypothesis)
    result.items["pr_gate_value"] = gate
    result.items["pr_gate"] = gate is EXPECT_PR
    result.items["runbook"] = hypothesis.get("runbook_cited") == EXPECT_RUNBOOK

    result.items.update(evidence_items(scenario, checks))
    return result
