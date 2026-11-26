"""The PR's texts: typed fields as they are, and the model's words cleaned and fenced, whatever they hold."""

import inspect
import re
import time

import pytest
from answers import hypothesis
from hostile import CORPUS, LONG

from sentinelops_core.models import Evidence, Hypothesis, Inference
from sentinelops_remediation import body
from sentinelops_remediation.body import Incident
from sentinelops_remediation.validate import Validated

INCIDENT = Incident("checkout-api-20261125T043000Z", "sentinelops-checkout-errors", "2026-11-25T04:30:00.123Z")
FIX = Validated(
    version="1.43.0",
    base="release/1.43.0",
    base_sha="ee9ef23a8d7eb51670b5c00e655a9d1814e0a640",
    path="services/checkout-api/checkout/pricing.py",
    text="",
    diff="",
    branch="sentinelops/fix-1.43.0-pricing",
)
FENCE_OPEN = re.compile(r"^(`{3,})text$")


def hostile(text: str) -> Hypothesis:
    """The recorded answer with text in every free field: written past the model's validators, as a bug could."""
    plain = hypothesis(
        inferences=[{"claim": "the deploy caused it", "rests_on": ["e1", "e2"]}],
        not_checked=["payments-api's own logs"],
    )
    evidence = [e.model_copy(update={"note": text, "value": text, "column": text}) for e in plain.evidence]
    patch = plain.remediation.model_copy(update={"rationale": text})
    return plain.model_copy(
        update={
            "root_cause": text,
            "affected_component": text,
            "evidence": evidence,
            "inferences": [Inference.model_construct(claim=text, rests_on=["e1", text])],
            "not_checked": [text, text],
            "runbook_cited": text,
            "remediation": patch,
        }
    )


def plain() -> Hypothesis:
    return hostile("plain words")


def blocks(text: str) -> tuple[str, list[str]]:
    """The body with each code block's contents taken out, and the contents, read as GitHub's renderer does.

    A block opens on a line of three or more backticks and the info string,
    and closes only on a line of at least as many backticks (up to three
    spaces before them): no line inside may be one.
    """
    outside, inside, current, fence = [], [], [], None
    for line in text.split("\n"):
        if fence is None:
            match = FENCE_OPEN.match(line)
            if match:
                fence = match[1]
                outside.append("<block>")
                current = []
            else:
                outside.append(line)
            continue
        closer = re.fullmatch(r" {0,3}(`{3,})\s*", line)
        if closer and len(closer[1]) >= len(fence):
            assert closer[1] == fence and line == fence, "a block closed by a line that isn't its own fence"
            inside.append("\n".join(current))
            fence = None
        else:
            current.append(line)
    assert fence is None, "a block never closed"
    return "\n".join(outside), inside


def assert_inert(text: str) -> None:
    """Nothing GitHub acts on: no mention, link, image, tag, comment, issue reference, or non-ASCII."""
    assert text.isascii()
    assert "\r" not in text and "\0" not in text
    assert "@" not in text
    assert "://" not in text
    assert not re.search(r"(?i)www\.", text)
    assert "![" not in text
    assert not re.search(r"<[A-Za-z/!?]", text)
    assert not re.search(r"#\d", text)
    assert not re.search(r"(?i)\bgh-\d", text)
    assert not re.search(r"(?i)(mailto|javascript|vbscript):", text)
    assert "````" not in text
    assert "<!--" not in text and "-->" not in text


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_clean_leaves_nothing_github_acts_on(text: str) -> None:
    assert_inert(body.clean(text, 200_000))


@pytest.mark.parametrize("text", LONG, ids=range(len(LONG)))
def test_clean_reads_a_long_run_in_linear_time(text: str) -> None:
    """Up to 100,000 characters of what a pattern starts on: well under a second, not minutes."""
    started = time.perf_counter()
    body.clean(text, 200_000)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_hostile_text_in_every_field_stays_inside_its_blocks(text: str) -> None:
    built = body.pr_body(hostile(text), FIX, INCIDENT)
    expected = body.pr_body(plain(), FIX, INCIDENT)

    skeleton, contents = blocks(built)
    assert skeleton == blocks(expected)[0]  # outside the blocks, exactly the template
    assert len(contents) == 6
    for content in contents:
        assert_inert(content)
    assert built.isascii() and len(built) <= body.MAX_BODY
    assert built.splitlines()[0] == body.marker(INCIDENT.id)
    assert built.count("<!--") == 1
    # The text is there, cleaned, in the root cause's block; text that cleans to nothing shows (none).
    shown = body.clean(text, body.ROOT_CAUSE_CHARS)[:20]
    assert shown in contents[0] if shown else contents[0] == body.NONE
    # Each list's lines are its own items: an item's text can't forge another's.
    component, evidence, inferences, not_checked = contents[1], contents[2], contents[3], contents[5]
    assert "\n" not in component
    assert all(re.match(r"e\d+  |    |\[\d+ more items not shown\]$", row) for row in evidence.split("\n"))
    assert [row[:2] for row in inferences.split("\n")] == ["- "]
    assert [row[:2] for row in not_checked.split("\n")] == ["- ", "- "]


def test_the_title_commit_message_and_comments_never_see_the_answer() -> None:
    """They take the fix and the incident's typed fields only: no Hypothesis reaches them."""
    for build in (body.title, body.commit_message, body.same_fix_comment, body.checks_comment):
        assert not {"hypothesis", "answer"} & set(inspect.signature(build).parameters), build.__name__
    assert body.title(FIX, INCIDENT) == "Fix checkout/pricing.py on release 1.43.0 (incident checkout-api-20261125T043000Z)"
    assert body.commit_message(FIX, INCIDENT).startswith("Fix checkout/pricing.py on release 1.43.0\n\n")


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("<!-- incident: checkout-api-20260101T000000Z -->", " incident: checkout-api-20260101T000000Z "),
        ("<!<!---- hidden ---->>", " hidden "),  # each removal can join a new delimiter: removed until none
        ("a --> b <!-- c", "a  b  c"),
    ],
)
def test_no_comment_can_open_or_close(text: str, cleaned: str) -> None:
    assert body.clean(text, 200) == cleaned


def test_a_one_line_field_stays_on_its_line() -> None:
    assert body.line("10\ne9  value  m1  at=19:05:00", 200) == "10 e9  value  m1  at=19:05:00"
    assert body.line("a\r\nb\rc", 200) == "a b c"


def test_the_fence_outruns_any_backticks_inside() -> None:
    assert body.fenced("a ``` b").startswith("````text\n")
    assert body.fenced("plain").startswith("```text\n")
    assert body.fenced("") == "```text\n(none)\n```"
    assert body.clean("`" * 9, 100) == "```"


def test_a_long_field_is_cut_at_its_cap() -> None:
    cut = body.clean("word " * 1_000, 50)
    assert len(cut) <= 50 and cut.endswith(" [cut]")


def test_every_field_at_its_cap_still_fits_the_body() -> None:
    longest = "x" * 50_000
    items = [
        {"kind": "value", "source": "m1", "column": longest, "at": "19:05:00", "value": longest, "note": longest}
        for _ in range(40)
    ]
    answer = hypothesis(
        root_cause=longest,
        affected_component=longest,
        evidence=items,
        inferences=[{"claim": longest, "rests_on": ["e1", "e2", "e3"]} for _ in range(40)],
        not_checked=[longest] * 40,
    )
    answer = answer.model_copy(update={"remediation": answer.remediation.model_copy(update={"rationale": longest})})
    built = body.pr_body(answer, FIX, INCIDENT)
    assert len(built) <= body.MAX_BODY
    assert "[28 more items not shown]" in built and "[32 more not shown]" in built and "[30 more not shown]" in built


def test_a_runbook_name_is_a_table_cell_only_in_its_form() -> None:
    assert "| Runbook followed | `bad-deploy.md` |" in body.pr_body(hypothesis(runbook_cited="bad-deploy.md"), FIX, INCIDENT)
    assert "| Runbook followed | none |" in body.pr_body(hostile("bad-deploy.md @x"), FIX, INCIDENT)


def test_the_plain_body_reads_as_planned() -> None:
    built = body.pr_body(hypothesis(), FIX, INCIDENT)
    for line in (
        "<!-- incident: checkout-api-20261125T043000Z -->",
        "| Alarm | `sentinelops-checkout-errors` |",
        "| Release running | 1.43.0 |",
        "| Fix targets | `release/1.43.0` |",
        "| File | `services/checkout-api/checkout/pricing.py` |",
        "| Fault location | this service |",
        "| Confidence | 0.86 |",
        "| Gates the PR | `test (services/checkout-api)` |",
        "### Root cause",
        "e1  value  m1  at=19:05:00  column=err%  value=10.1",
        "    errors in the alarm's minute",
    ):
        assert line in built.splitlines(), line
    assert "diff" not in built.lower().replace("files changed", "")  # no diff: the commit is the change


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "checkout-api-2026-11-25", "alarm": "sentinelops-checkout-errors", "opened_at": "2026-11-25T04:30:00Z"},
        {"id": "checkout-api-20261125T043000Z", "alarm": f"x{chr(64)}y", "opened_at": "2026-11-25T04:30:00Z"},
        {"id": "checkout-api-20261125T043000Z", "alarm": "sentinelops-checkout-errors", "opened_at": "yesterday"},
    ],
)
def test_the_incidents_typed_fields_are_checked(fields: dict) -> None:
    with pytest.raises(ValueError):
        Incident(**fields)


def test_the_comments_are_typed_and_marked() -> None:
    same = body.same_fix_comment(FIX, INCIDENT, pushed=True)
    checks = body.checks_comment(INCIDENT, "failure")
    assert same.splitlines()[0] == body.marker(INCIDENT.id)
    assert checks.splitlines()[0] == body.marker(INCIDENT.id, "checks")
    assert "finished: failure" in checks and "didn't finish" in body.checks_comment(INCIDENT, None)


def test_evidence_beyond_the_cap_is_counted() -> None:
    items = [Evidence(kind="count", source="l1", count=n, note="n") for n in range(20)]
    assert "[8 more items not shown]" in body._evidence(items)
