"""Each scenario's rubric, in code: is the diagnosis right, and does its evidence carry it?

Items, each scored on its own (BUILD_PLAN.md, Phase 3 design):
  names_culprit     root_cause names the change that caused it
  names_mechanism   root_cause says how that change produces the symptom
  blames_no_other   root_cause blames no other change: a suspect named in a
                    sentence that rules it out ("not", "separate", "already
                    reverted") is accounted for, not blamed
  location          affected_component is this service, not the upstream
  pr_gate           Hypothesis.can_open_pr, read through the property, is what
                    the scenario expects
  runbook           runbook_cited is what the scenario expects: null, while
                    there's no runbook search
  evidence_change   an evidence item names the culprit with its time
  evidence_failure  an evidence item shows the mechanism: the logged error, or
                    the pool's wait or use, with a value
  evidence_symptom  an evidence item gives the alarm's own metric with a value

The last three keep a Hypothesis that says less from scoring better. `diagnosis`
is the first four together; `confidently_wrong` is a wrong diagnosis that
would still pass the PR gate.

Rules match words, so they're brittle to phrasing: the hand audit records its
own verdict on each diagnosis, and the two are compared.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from evals.scenarios import ERRORS, LATENCY, POOL_WAIT, Fault, Scenario
from models import Hypothesis

# Arrows and dashes the model writes, as ASCII, so the rules can stay ASCII.
_ASCII = {0x2192: "->", 0x2013: "-", 0x2014: "-", 0x2212: "-", 0x2248: "~", 0x00D7: "x"}
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_RULED_OUT = re.compile(
    r"\bnot\b|n't\b|\bno longer\b|\brule[sd]? out\b|\bunrelated\b|\bseparate\b|\bresolved\b|\bearlier\b"
    r"|\balready\b|\brather than\b|\binstead of\b|\breverted\b|\bprior\b|\bprevious\b",
    re.I,
)
_DECOY_KEYS = r"LOG_SAMPLE_RATE|CART_TTL|PRODUCT_CACHE_TTL|RECOMMENDATIONS_ENABLED"
_NUMBER = re.compile(r"\d")


@dataclass(frozen=True)
class FaultRules:
    culprit: re.Pattern[str]
    mechanism: tuple[re.Pattern[str], ...]  # every one must match
    wrong_suspects: re.Pattern[str]
    failure: re.Pattern[str]
    failure_needs_value: bool  # a wait or a count needs its number; an error type is itself the observation


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
        failure=re.compile(r"TypeError|NoneType|apply_promo|pricing\.py"),
        failure_needs_value=False,
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
        failure=re.compile(r"pool[_ ]?wait|pool_active|connections? (in use|active)|in-use connections", re.I),
        failure_needs_value=True,
    ),
}
_SYMPTOM = {
    ERRORS: (re.compile(r"err%|error rate|errors?\b|5\d\d", re.I), re.compile(r"\d+(\.\d+)?\s*%|\b0\.\d+")),
    LATENCY: (re.compile(r"latency|p99", re.I), re.compile(r"\d[\d,]*(\.\d+)?\s*(ms|s)\b")),
    POOL_WAIT: (re.compile(r"pool[_ ]?wait", re.I), re.compile(r"\d[\d,]*(\.\d+)?\s*ms\b")),
}
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


def score(scenario: Scenario, hypothesis: dict[str, Any]) -> RubricResult:
    rules = RULES[scenario.fault]
    result = RubricResult()
    cause = ascii_text(hypothesis.get("root_cause") or "")
    evidence = [ascii_text(e) for e in hypothesis.get("evidence") or []]
    component = ascii_text(hypothesis.get("affected_component") or "")

    result.items["names_culprit"] = bool(rules.culprit.search(cause))
    result.items["names_mechanism"] = all(p.search(cause) for p in rules.mechanism)
    wrong = blamed(cause, rules.wrong_suspects)
    result.items["blames_no_other"] = not wrong
    if wrong:
        result.notes["blames_no_other"] = "blames " + ", ".join(sorted(set(wrong)))
    in_service = re.search(r"checkout|pool", component, re.I) is not None
    upstream = re.search(r"payments", component, re.I) is not None
    result.items["location"] = in_service and not upstream

    gate = Hypothesis.model_validate(hypothesis).can_open_pr
    result.items["pr_gate_value"] = gate
    result.items["pr_gate"] = gate is EXPECT_PR
    result.items["runbook"] = hypothesis.get("runbook_cited") == EXPECT_RUNBOOK

    result.items["evidence_change"] = any(rules.culprit.search(e) and scenario.culprit_at in e for e in evidence)
    result.items["evidence_failure"] = any(
        rules.failure.search(e) and (not rules.failure_needs_value or _NUMBER.search(e)) for e in evidence
    )
    what, value = _SYMPTOM[scenario.alarm]
    result.items["evidence_symptom"] = any(what.search(e) and value.search(e) for e in evidence)
    return result
