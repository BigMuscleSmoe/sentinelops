"""Each scenario's rubric, in code: is the diagnosis right, and does its evidence carry it?

Items, each scored on its own (BUILD_PLAN.md, Phase 3 design):
  names_culprit     root_cause names the change that caused it
  names_mechanism   root_cause says how that change produces the symptom
  blames_no_other   root_cause blames no other change: a suspect named in a
                    clause that rules it out ("not", "neither ... nor",
                    "separate", "already reverted") is accounted for, not
                    blamed, and so is one named only as never reached ("before
                    any payments-api call", "before it reached payments-api").
                    A ruling-out covers its own clause, not the
                    sentence: "pool exhaustion caused the timeouts, while
                    database time was normal" blames the pool. Another
                    fault's culprit by its bare noun ("the pool", "the
                    database") is blamed only where its clause says it
                    caused the failure ("the pool caused the errors", "due
                    to the database"); a plain mention ("database time
                    rose 2 ms") isn't blame. A clause that tells of an
                    earlier incident is ruled out: its change "rolled
                    back", "reverted", or "resolved", or put well before
                    the onset ("hours before", "88 minutes before", "at
                    22:28, before this alarm"). Said of "it" in the clause
                    that follows, that covers the clause before as well
                    ("1.43.0 caused 500s at 22:28, but it was rolled
                    back"). A time alone rules nothing out
  location          affected_component and fault_location are where the fault
                    is: this service for a pool fault or a bad deploy, and
                    payments-api, upstream, for a downstream timeout
  pr_gate           Hypothesis.can_open_pr, read through the property, is what
                    the scenario expects: True for a bad deploy, whose fix is
                    code (a patch the run's check kept), False for a pool fault,
                    a config change outside the code, and for a downstream
                    timeout (Phase 4 design, decision 10)
  runbook           runbook_cited is the fault's own runbook
                    (db-pool-exhaustion.md, bad-deploy.md, or
                    downstream-timeout.md), exactly, after the run's own
                    citation check; apart from `diagnosis`
  evidence_change   an evidence item cites the culprit's own deploy record, by
                    a field that shows the change (its version, or the changed
                    key), and holds there; for a downstream timeout, which has
                    no culprit record, `none` items on deploy history that
                    hold and together rule out every kind of record (one with
                    no column, or one for deploys and one for config changes),
                    each over a window containing the 5 minutes before the
                    onset
  evidence_failure  an evidence item that holds shows the mechanism: a logged
                    error naming it, or the pool's wait or use, or payments-api's
                    latency or timeouts, as a number
  evidence_symptom  an evidence item that holds gives the alarm's own metric as
                    a number, from a metrics table or the alarm message
  evidence_health   a downstream timeout's only: an item that holds gives one of
                    this service's own signals (pool wait, database time, or
                    err%) as a number

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
from datetime import timedelta
from typing import Any

from checker.checks import ItemCheck, Verdict, field_of, is_number
from checker.sources import RunSources
from evals.scenarios import ERRORS, LATENCY, POOL_WAIT, Fault, Scenario
from sentinelops_core.models import FaultLocation, Hypothesis

# Arrows and dashes the model writes, as ASCII, so the rules can stay ASCII.
_ASCII = {0x2192: "->", 0x2013: "-", 0x2014: "-", 0x2212: "-", 0x2248: "~", 0x00D7: "x"}
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_RULED_OUT = re.compile(
    r"\bnot\b|n't\b|\bno longer\b|\brule[sd]? out\b|\bunrelated\b|\bseparate\b|\bresolved\b|\bearlier\b"
    r"|\balready\b|\brather than\b|\binstead of\b|\breverted\b|\bprior\b|\bprevious\b"
    r"|\bunchanged\b|\bhealthy\b"
    # "Neither X nor Y" rules out both. "Nor" counts alone, so the second half
    # is still ruled out when a clause break falls between the two.
    r"|\bneither\b|\bnor\b"
    r"|\bstayed flat\b|\bunaffected\b|\bno changes?\b"
    # A state, not an adjective: "a steady climb" or "above normal" rules nothing out.
    r"|\b(?:is|are|was|were|stays|stay|stayed|remains|remain|remained|looks|look|looked) normal\b|\bnormal range\b"
    r"|\b(?:is|are|was|were|holds|hold|held|stays|stay|stayed|remains|remain|remained) steady\b",
    re.I,
)
# A ruling-out covers its clause. Clauses end at a semicolon or a colon, before a
# contrast or a consequence (while, whereas, but, although, though, yet, so), and
# before ", not".
_CLAUSE = re.compile(r"\s*[;:]\s+|,?\s+(?=(?:while|whereas|but|although|though|yet|so)\b)|,\s+(?=not\b)", re.I)
# A suspect named only as never reached, read from the words before it: "before
# any payments-api call", "before any database or payments-api work", "before the
# request reaches payments-api", "before it reached payments-api", "never reached
# payments-api". It excuses the suspect it names and nothing else in the clause.
# The past tense reads the same as the present: what failed before it reached
# a suspect never got there. No saved answer uses it for an arrival.
# A short list after "reaching" excuses its last suspect too ("fail with 500
# before reaching the DB pool or payments-api", 2026-11-23), but only after a
# failure in the same clause (fail, raise, throw, crash, an error, an
# exception, a 5xx): "spend 2 s in pricing before reaching the DB pool and
# payments-api" is an arrival.
_NOT_REACHED = re.compile(
    r"\bbefore any\b[^,;:]{0,40}$"
    r"|\bbefore\b[^,;:]{0,30}\breach(?:es|ed|ing)?\s+(?:the\s+)?$"
    r"|\bnever\s+reach(?:es|ed|ing)?\s+(?:the\s+)?$"
    r"|\b(?:fail\w*|rais(?:es|ed|ing)|throw\w*|crash\w*|errors?|errored|exceptions?|5\d\d)\b[^,;:]{0,60}?"
    r"\bbefore\s+reach(?:es|ed|ing)?\s+(?:the\s+)?(?:[\w.-]+\s+){1,3}(?:or|and)\s+(?:the\s+)?$",
    re.I,
)
# An earlier incident, over before this one, isn't blamed for this one. The
# clause says its change was rolled back, reverted, or resolved (the last two
# rule out by themselves, above), or puts it well before the onset: "well
# before", "hours before", "88 minutes before" (10 minutes or more: a change
# here shows within a minute). "Before the onset" or "before this alarm" alone
# is how any cause is told, so it counts only right after a verb of happening
# ("happened before the onset") or a clock time ("at 22:28, before this
# alarm"), and not where the clause goes on to say what that caused ("a deploy
# at 23:56, before the alarm, caused the slowdown").
_OVER = re.compile(r"\broll(?:ed|ing|s)?[- ]?back\b|\breverted\b|\bresolved\b", re.I)
_WELL_BEFORE = re.compile(
    r"\b(?:well|long|far)\s+before\b|\b(?:hours?|hrs?|days?)\s+before\b|(?<![\d.])\d+(?:\.\d+)?\s*h\s+before\b"
    r"|(?<![\d.])\d{2,}(?:\.\d+)?\s*(?:minutes?|mins?)\s+before\b",
    re.I,
)
_BEFORE_THIS = re.compile(
    r"(?:\b(?:happened|occurred|took place|ended|finished|completed|was over|were over)\s+"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:Z|UTC)?,?\s+)"
    r"before\s+(?:(?:the|this|its)\s+)?(?:(?:current|latency)\s+)?(?:onset|alarm|incident|outage|slowdown)\b",
    re.I,
)
# The clause after may say it of the one before: "..., but it was rolled back
# well before onset", "It was reverted at 22:29." It opens by referring back
# and names no suspect of its own.
_REFERS_BACK = re.compile(
    r"(?:(?:but|and|though|although|yet|while|whereas|so)\s+)?"
    r"(?:it|which|they|both|that|this|these|those|was|were|had|has"
    r"|the\s+(?:deploy(?:ment)?|release|rollout|rollback|change|version|incident|fault|issue|problem|bug|regression))\b",
    re.I,
)
# Another fault's culprit by its bare noun, blamed only where the clause says
# it caused the failure: the noun, then a causing verb or "the cause" ("the
# pool caused the errors", "the database was the root cause"); a causal phrase,
# then the noun ("due to the database", "caused by contention in the pool",
# "the root cause is the database"); or the noun right after "because"
# ("because the pool ..."). Up to 40 characters may stand between the two,
# with no comma, bracket, "and", or "or", and without the fault's own culprit,
# which the verb then belongs to ("the pool was at 10 of 20 when payments-api
# caused the timeouts"). A passive isn't the noun's doing ("the database errors
# were caused by 1.43.0"). A clock time's colon ("at 23:56") is no punctuation.
_IN_CLAUSE = r"(?:[^,;:()]|(?<=\d):(?=\d))"
_GAP = rf"(?:(?!\b(?:and|or)\b){_IN_CLAUSE}){{0,40}}?"
_SAYS_IT_CAUSED = (
    r"(?:(?<!\bis )(?<!\bare )(?<!\bwas )(?<!\bwere )(?<!\bbeen )(?<!\bbeing )"
    r"\b(?:caused|triggered|introduced)\b(?!\s+by\b)"
    r"|\b(?:caus(?:es?|ing)|culprit|responsible|at fault|to blame|led to|leads? to|leading to"
    r"|result(?:s|ed|ing)? in|broke|breaks?|breaking|triggers?|introduces?)\b)"
)
_THEN_CAUSES = re.compile(rf"(?P<gap>{_GAP}){_SAYS_IT_CAUSED}", re.I)
_GOES_ON_TO_CAUSE = re.compile(_SAYS_IT_CAUSED, re.I)
_CAUSED_BY = re.compile(
    r"\b(?:due to|because of|owing to|thanks to|(?:caused|causing|triggered|introduced|driven) by"
    r"|result(?:s|ed|ing)? (?:of|from)|stems? from|stemm(?:ed|ing) from|comes? from|came from|coming from"
    r"|traced (?:back )?to|blamed? on|(?:cause|culprit|fault) (?:is|was|lies in|lies with|lay in|lay with))\b"
    rf"(?P<gap>{_GAP})$",
    re.I,
)
_BECAUSE = re.compile(r"\bbecause\s+(?:(?:the|a|an|its|this|that)\s+)?(?:[\w'-]+\s+)?$", re.I)
# After "because", a noun that only names a metric or a record is a plain
# mention: "because pool wait stayed under 1 ms", "because the deploy history
# shows nothing".
_NAMES_A_READING = re.compile(
    r"\s+(?:wait|active|time|quer(?:y|ies)|latency|p\d\d|timeouts?|calls?|connections?|usage|use|metrics?"
    r"|columns?|work|writes?|history|records?)\b",
    re.I,
)
# A bare noun the clause doesn't assert anything of: "no deploy caused it",
# "nothing in the pool or the database", "if the pool were the cause". "No
# promo" is the service's own phrase, not a denial.
_NOT_ASSERTED = re.compile(rf"\b(?:no|nothing|none|if|whether|unless)\b(?! promo){_IN_CLAUSE}{{0,30}}$", re.I)
_BARE_POOL = r"pools?"
_BARE_DATABASE = r"(?:database|db)(?!\s+(?:connection|pool))"  # "the DB connection pool" is the pool
_BARE_PAYMENTS = r"payments?"
_BARE_DEPLOY = r"deploy(?:ment)?|release|rollout"
_DECOY_KEYS = r"LOG_SAMPLE_RATE|CART_TTL|PRODUCT_CACHE_TTL|RECOMMENDATIONS_ENABLED"
_SHOWN = (Verdict.HOLDS, Verdict.LOOSE)  # what an item got right; a loose one's extra words aren't
_STATES_A_NUMBER = ("value", "range", "all")  # a count states how many, a first or last a time
_QUIET_BEFORE_ONSET = timedelta(minutes=5)  # what a none must cover when nothing changed
RECORD_KINDS = frozenset({"deploy", "config"})  # every kind of record deploy history holds


@dataclass(frozen=True)
class FaultRules:
    culprit: re.Pattern[str]
    mechanism: tuple[re.Pattern[str], ...]  # every one must match
    wrong_suspects: re.Pattern[str]
    # The other faults' culprits by their bare nouns: blame only with a cause word.
    bare_suspects: re.Pattern[str]
    # The mechanism in the cited data: a logged value that names it (an error type
    # is itself the observation), or a column whose number shows it (a wait).
    failure_value: re.Pattern[str] | None
    failure_column: re.Pattern[str] | None
    runbook: str  # the runbook a right answer follows, by its file name in runbooks/
    # Where the fault is, and what affected_component must name.
    location: FaultLocation = FaultLocation.THIS_SERVICE
    component: re.Pattern[str] = re.compile(r"checkout|pool", re.I)
    expect_pr: bool = True  # what Hypothesis.can_open_pr should say
    # A fault with no culprit record: its evidence_change is the history's
    # silence before the onset, and evidence_health one of this service's signals.
    no_culprit_record: bool = False
    health_column: re.Pattern[str] | None = None


_POOL_BLAME = r"POOL_MAX_SIZE|pool[_ ]max|pool size|pool[- ]exhaust|exhausted pool|connection pool"

RULES = {
    Fault.BAD_DEPLOY: FaultRules(
        culprit=re.compile(r"1\.43\.0|9c41e7b|simplify promo pricing", re.I),
        mechanism=(
            re.compile(r"promo", re.I),
            re.compile(r"\bNone\b|NoneType|(?i:null|TypeError|subscript|dereferenc)"),
        ),
        wrong_suspects=re.compile(
            rf"POOL_MAX_SIZE|pool[_ ]max|pool size|pool[- ]exhaust|connection pool|payments-api|{_DECOY_KEYS}", re.I
        ),
        bare_suspects=re.compile(rf"\b(?:{_BARE_POOL}|{_BARE_DATABASE}|{_BARE_PAYMENTS})\b", re.I),
        failure_value=re.compile(r"TypeError|NoneType|apply_promo|pricing\.py"),
        failure_column=None,
        runbook="bad-deploy.md",
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
        # No bare "deploy" or "release" here: the pool's own culprit is a change an answer may call one.
        bare_suspects=re.compile(rf"\b(?:{_BARE_DATABASE}|{_BARE_PAYMENTS})\b", re.I),
        failure_value=None,
        failure_column=re.compile(r"^pool_(wait|active)"),
        runbook="db-pool-exhaustion.md",
        # The fix is reverting a config change made outside the code: no patch, no PR.
        expect_pr=False,
    ),
    Fault.DOWNSTREAM_TIMEOUT: FaultRules(
        culprit=re.compile(r"payments?[-_ ]?api|payment provider", re.I),
        mechanism=(re.compile(r"time[sd]?[- ]?out|slow|latenc|degrad|\b504\b|did not respond|didn't respond", re.I),),
        # Naming the timeout setting is part of a right answer; blaming a change to it isn't.
        # "promo" alone isn't a suspect: the 504s come with and without one.
        wrong_suspects=re.compile(
            rf"{_POOL_BLAME}|slow quer|database (is |was )?slow|slow database|1\.43\.0|9c41e7b|TypeError|apply_promo"
            rf"|promo pricing|PAYMENTS_TIMEOUT_MS (was )?(change|lower|reduc|cut)\w*|{_DECOY_KEYS}",
            re.I,
        ),
        bare_suspects=re.compile(rf"\b(?:{_BARE_POOL}|{_BARE_DATABASE}|{_BARE_DEPLOY})\b", re.I),
        failure_value=re.compile(r"UpstreamTimeout|did not respond|payment provider timed out|\b504\b"),
        failure_column=re.compile(r"^payments_"),
        runbook="downstream-timeout.md",
        location=FaultLocation.UPSTREAM,
        component=re.compile(r"payments", re.I),
        expect_pr=False,
        no_culprit_record=True,
        health_column=re.compile(r"^(pool_wait_p99_ms|db_query_p50_ms|err%)$"),
    ),
}
# Each alarm's own metric, as the metrics table heads it; the alarm message's datapoints are it too.
_SYMPTOM = {ERRORS: "err%", LATENCY: "p99_ms", POOL_WAIT: "pool_wait_p99_ms"}


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


def clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE.split(sentence) if c.strip()]


def blamed(text: str, rules: FaultRules) -> list[str]:
    """The suspects a root cause blames, clause by clause.

    A named suspect is blamed in a clause that doesn't rule it out, unless it's
    named only as never reached. A clause that blames none of those is read for
    the bare nouns, each blamed only if the clause says it caused the failure.
    A clause that tells of an earlier incident blames nothing, and neither does
    the clause before one that says so of "it".
    """
    parts = [c for s in sentences(text) for c in clauses(s)]
    out: list[str] = []
    for c, after in zip(parts, [*parts[1:], ""]):
        if _RULED_OUT.search(c) or _earlier(c) or _says_it_was_earlier(after, rules):
            continue
        named = list(rules.wrong_suspects.finditer(c))
        found = [m.group() for m in named if not _NOT_REACHED.search(c[: m.start()])]
        out += found or _bare_blamed(c, rules, named)
    return out


def _earlier(clause: str) -> bool:
    """Whether a clause tells of an earlier incident: rolled back, reverted, or resolved, or well before the onset."""
    if _OVER.search(clause) or _WELL_BEFORE.search(clause):
        return True
    return any(not _GOES_ON_TO_CAUSE.search(clause[m.end() :]) for m in _BEFORE_THIS.finditer(clause))


def _says_it_was_earlier(clause: str, rules: FaultRules) -> bool:
    """Whether a clause says the one before it was an earlier incident: "but it was rolled back well before onset"."""
    return (
        _REFERS_BACK.match(clause.lstrip()) is not None
        and not rules.wrong_suspects.search(clause)
        and _earlier(clause)
    )


def _bare_blamed(clause: str, rules: FaultRules, named: list[re.Match[str]]) -> list[str]:
    """The bare nouns a clause says caused the failure; part of a named suspect is that suspect."""
    out = []
    for m in rules.bare_suspects.finditer(clause):
        before, after = clause[: m.start()], clause[m.end() :]
        if any(m.start() < n.end() and n.start() < m.end() for n in named):
            continue
        if _NOT_REACHED.search(before) or _NOT_ASSERTED.search(before):
            continue
        then, by = _THEN_CAUSES.match(after), _CAUSED_BY.search(before)
        if (
            (then and not rules.culprit.search(then["gap"]))
            or (by and not rules.culprit.search(by["gap"]))
            or (_BECAUSE.search(before) and not _NAMES_A_READING.match(after))
        ):
            out.append(m.group())
    return out


def shown(check: ItemCheck) -> list[str]:
    """What an item's column holds at the entries it cites, as text; a list's elements joined."""
    out = []
    for entry in check.entries:
        value = field_of(entry, check.column)
        out.append(" ".join(map(str, value)) if isinstance(value, list) else str(value))
    return out


def shows_a_number(check: ItemCheck) -> bool:
    return check.kind in _STATES_A_NUMBER and any(is_number(field_of(e, check.column)) for e in check.entries)


def kinds_ruled_out(scenario: Scenario, check: ItemCheck, item: Any) -> frozenset[str]:
    """The kinds of record a none on deploy history rules out as the fault began.

    It must hold, over a window containing the 5 minutes before the onset. With
    no column it says the window holds no record at all, and so does `kind` with
    no value: every kind. `kind = deploy` or `kind = config` rules out that one.
    Any other column says less than that no record of a kind exists: no kind.
    """
    if not (check.verdict is Verdict.HOLDS and check.kind == "none" and (check.source or "").startswith("d")):
        return frozenset()
    if not isinstance(item, dict) or scenario.onset is None:
        return frozenset()
    times = RunSources(scenario.fired_at)
    try:  # the schema requires both on a none
        start, _ = times.moment(str(item["start"]))
        end, end_width = times.moment(str(item["end"]))
    except (KeyError, ValueError):
        return frozenset()
    if not (start <= scenario.onset - _QUIET_BEFORE_ONSET and end + end_width >= scenario.onset):
        return frozenset()
    column, value = item.get("column"), item.get("value")
    if not column:
        return RECORD_KINDS
    if column != "kind":
        return frozenset()
    if value in (None, ""):
        return RECORD_KINDS
    return RECORD_KINDS & {str(value).strip().strip("'\"").casefold()}


def evidence_items(scenario: Scenario, checks: list[ItemCheck], evidence: list[Any] | None = None) -> dict[str, bool]:
    """The evidence items, from the checker's verdicts, the entries each item cites,
    and, for a none's window, what the item states."""
    rules = RULES[scenario.fault]
    right = [c for c in checks if c.verdict in _SHOWN]
    items = {f"e{n}": item for n, item in enumerate(evidence or [], 1)}

    if rules.no_culprit_record:
        # Nothing changed: one none for every kind of record, or several that cover them between them.
        ruled_out = frozenset().union(*(kinds_ruled_out(scenario, c, items.get(c.id)) for c in checks))
        change = ruled_out == RECORD_KINDS
    else:
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
    out = {"evidence_change": change, "evidence_failure": failure, "evidence_symptom": symptom}
    if rules.health_column is not None:
        health = rules.health_column
        out["evidence_health"] = any(
            (c.source or "").startswith("m") and bool(health.search(c.column or "")) and shows_a_number(c) for c in right
        )
    return out


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
    wrong = blamed(cause, rules)
    result.items["blames_no_other"] = not wrong
    if wrong:
        result.notes["blames_no_other"] = "blames " + ", ".join(sorted(set(wrong)))
    named = rules.component.search(component) is not None
    located = hypothesis.get("fault_location") == rules.location
    if rules.location is FaultLocation.THIS_SERVICE:
        result.items["location"] = named and re.search(r"payments", component, re.I) is None and located
    else:
        result.items["location"] = named and located

    gate = pr_gate(hypothesis)
    result.items["pr_gate_value"] = gate
    result.items["pr_gate"] = gate is rules.expect_pr
    result.items["runbook"] = hypothesis.get("runbook_cited") == rules.runbook

    evidence = hypothesis.get("evidence")
    result.items.update(evidence_items(scenario, checks, evidence if isinstance(evidence, list) else None))
    return result
