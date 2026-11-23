"""The agent loop end to end, through Strands, against a scripted Messages API."""

import asyncio
import json
import logging
import time
from typing import Any, Literal

import pytest
from strands.models._native_output import _UNSUPPORTED_MECHANISMS

from fake_anthropic import ACCESS_TOKEN, FakeAnthropic, Reply, answer, signed_in, text, thinking, tool_use
from answers import DEPLOY_EVIDENCE, PATCH, PRICING_1_42_0, PRICING_1_43_0, item
from evals.arms import ARMS
from guards import Limits
from hooks import COUNTED_FIELDS, count_margin
from sentinelops_core import code, patches
from sentinelops_core.models import Hypothesis, StopReason
from pricing import price_for
from providers import Settings, build_model
from runner import ANSWER_AS_TEXT, RunReport, investigate, system_prompt
from sentinelops_tools.runbooks import SHOWN_SECTIONS, Hit, parse_results, render
from stubs import TOOL_FIXTURES, load

HAIKU = price_for("anthropic", "claude-haiku-5-5")
SETTINGS = Settings()
DIAGNOSIS = {
    "root_cause": "Release 1.43.0 removed the null check in apply_promo, so checkouts without a promo fail.",
    "confidence": 0.9,
    "evidence": DEPLOY_EVIDENCE,
    "affected_component": "checkout-api",
    "fault_location": "this_service",
}


class Tools:
    """Stand-ins with the real tools' names, counting their runs."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self.runs: list[str] = []
        self.sleep_s = sleep_s

    def all(self) -> list:
        def get_metrics(minutes: int = 10, period_seconds: Literal[10, 60] = 10) -> str:
            """Metrics.

            Args:
                minutes: How far back.
                period_seconds: The period.
            """
            self.runs.append("get_metrics")
            time.sleep(self.sleep_s)
            return "time  req  err%\n19:05:00*  276  10.1"

        def get_recent_logs(minutes: int = 10, errors_only: bool = True, limit: int = 20) -> str:
            """Logs.

            Args:
                minutes: How far back.
                errors_only: Failures only.
                limit: How many.
            """
            self.runs.append("get_recent_logs")
            return '{"error_type":"TypeError","stack":["checkout/pricing.py:15 in apply_promo"]}'

        return [get_metrics, get_recent_logs]


def run(fake: FakeAnthropic, tools: Tools | None = None, *, settings: Settings = SETTINGS, price=HAIKU, **kwargs) -> RunReport:
    model = build_model(settings, http_client=fake.client(), credentials=signed_in())
    return asyncio.run(
        investigate(
            "An alarm fired on checkout-api.",
            model=model,
            tools=(tools or Tools()).all(),
            price=price,
            max_output_tokens=settings.max_output_tokens,
            **kwargs,
        )
    )


def test_a_run_ends_with_the_models_hypothesis() -> None:
    fake = FakeAnthropic(
        [
            Reply([thinking("Errors first."), tool_use("get_metrics", {"minutes": 5})]),
            Reply([tool_use("get_recent_logs")]),
            answer(DIAGNOSIS),
        ]
    )
    tools = Tools()

    report = run(fake, tools)

    assert report.failure is None
    assert report.hypothesis is not None
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    assert report.hypothesis.halted_by is None
    assert tools.runs == ["get_metrics", "get_recent_logs"]
    assert report.tool_calls == ["get_metrics", "get_recent_logs"]
    assert len(fake.requests) == len(report.model_calls) == 3


def test_the_model_gets_the_settings_and_the_prompt() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])
    run(fake)

    request = fake.requests[0]
    body = request["body"]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 16_000
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Haiku 5.5 rejects sampling parameters with a 400.
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert "".join(block["text"] for block in body["system"]) == system_prompt()
    assert [t["name"] for t in body["tools"]] == ["get_metrics", "get_recent_logs", "Hypothesis"]


def test_every_tool_result_ends_with_the_budget() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)]
    )
    run(fake)

    results = fake.tool_results(2)
    assert [r.splitlines()[-1].split(",")[0] for r in results] == ["[budget: 14 steps", "[budget: 13 steps"]
    assert all(r.splitlines()[-1].endswith("s remaining]") and ", $0.0" in r for r in results)


def test_every_tool_result_is_labeled_as_a_source_and_its_log_lines_numbered() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), Reply([tool_use("get_metrics")]), answer(DIAGNOSIS)]
    )
    run(fake)

    metrics, logs, again = fake.tool_results(3)
    assert metrics.startswith("[source m1]\ntime  req  err%\n")
    assert logs.startswith('[source l1]\n[1] {"error_type":"TypeError"')
    assert again.startswith("[source m2]\n")


# A leak like the one that cost deploy-1029-0156-r2 its run, from before strict tool use: the
# closing tag inside the string, swallowing the field after it. Cut, it would still lack its
# confidence, so the trim leaves it, and it's sent back.
LEAKED = {
    **{k: v for k, v in DIAGNOSIS.items() if k != "confidence"},
    "root_cause": DIAGNOSIS["root_cause"] + '</root_cause>\n<parameter name="confidence">0.9',
}


def test_a_malformed_answer_is_sent_back_twice_then_the_run_halts() -> None:
    fake = FakeAnthropic([answer(LEAKED), answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert len(fake.requests) == 3, "no fourth call after the third rejection"
    (rejection,) = fake.tool_results(1)
    assert "holds tool-call markup ('</root_cause>')" in rejection
    assert report.hypothesis.halted_by is StopReason.RESEND_LIMIT
    assert "resend limit (2 resends of a malformed answer)" in report.hypothesis.not_checked[0]
    assert not report.hypothesis.can_open_pr


def test_an_answer_fixed_on_its_last_resend_is_kept() -> None:
    fake = FakeAnthropic([answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.halted_by is None
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]


def test_a_model_reporting_a_halt_with_no_evidence_fails_the_run() -> None:
    # A halt may carry no evidence, so this passes the answer's check; cleared of its
    # halt, it's an answer with no evidence, and nothing can resend it by then.
    fake = FakeAnthropic([answer({**DIAGNOSIS, "evidence": [], "halted_by": "step_limit"})])

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure.startswith("ValidationError")


def test_each_call_is_priced_from_its_own_usage_and_never_beyond_its_reservation() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)])
    report = run(fake)

    for call, request in zip(report.model_calls, fake.requests):
        # The fake bills one token per byte, the most the bound allows for,
        # and counts each prompt a few bytes short: no max_tokens or stream.
        assert call.prompt_tokens == request["bytes"]
        assert call.counted_tokens < call.prompt_tokens < call.limit_tokens < call.bound_tokens
        assert call.reserved_usd >= call.cost_usd
    assert report.cost_usd == pytest.approx(sum(c.cost_usd for c in report.model_calls))
    assert report.input_tokens == sum(r["bytes"] for r in fake.requests)


def test_the_sixteenth_tool_call_halts_the_run() -> None:
    # Small prompts, so the token limit can't trip first.
    fake = FakeAnthropic([Reply([tool_use("get_metrics")], input_tokens=1_000) for _ in range(16)] + [answer(DIAGNOSIS)])
    tools = Tools()

    report = run(fake, tools)

    assert report.hypothesis.halted_by is StopReason.STEP_LIMIT
    assert report.hypothesis.confidence == 0.0
    assert tools.runs == ["get_metrics"] * 15, "the 16th call is refused before the tool runs"
    assert len(fake.requests) == 16, "and the model call after it is never sent"


def test_a_call_that_could_pass_the_cost_cap_is_never_sent() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    report = run(fake, limits=Limits(max_cost_usd=0.005))  # the first call could cost ~$0.0098

    assert fake.requests == []
    assert len(fake.count_requests) == 1, "counted once, refused, and never retried"
    assert report.hypothesis.halted_by is StopReason.COST_LIMIT
    assert report.cost_usd == 0.0


def test_the_cost_cap_holds_across_a_run() -> None:
    # 60k-token prompts: each call costs ~$0.006, and the next could cost ~$0.02.
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")], input_tokens=60_000) for _ in range(10)] + [answer(DIAGNOSIS)]
    )

    report = run(fake, limits=Limits(max_cost_usd=0.03))

    assert report.hypothesis.halted_by is StopReason.COST_LIMIT
    assert len(fake.requests) == len(report.model_calls) == 2
    assert report.cost_usd <= 0.03
    assert "$0.03" in report.hypothesis.not_checked[0]


def test_the_wall_clock_cuts_off_a_slow_tool() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), answer(DIAGNOSIS)])

    report = run(fake, Tools(sleep_s=2.0), limits=Limits(max_wall_clock_s=0.5))

    assert report.hypothesis.halted_by is StopReason.WALL_CLOCK
    assert report.seconds < 1.5


def test_a_refusal_ends_the_run_without_a_hypothesis() -> None:
    fake = FakeAnthropic([Reply([text("I can't help with that.")], stop_reason="refusal")])

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure == "The model refused to go on with the investigation."
    assert len(report.model_calls) == 1 and report.cost_usd > 0, "a refusal is still paid for"


def test_a_model_that_stops_without_a_hypothesis_is_made_to_give_one() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)]
    )

    report = run(fake)

    assert fake.requests[2]["body"]["tool_choice"] == {"type": "tool", "name": "Hypothesis"}
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]


def test_each_call_is_counted_on_the_request_it_sends() -> None:
    fake = FakeAnthropic(
        [Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)]
    )

    run(fake)

    assert len(fake.count_requests) == len(fake.requests) == 3
    for counted, sent in zip(fake.count_requests, fake.requests):
        assert counted["body"] == {key: value for key, value in sent["body"].items() if key in COUNTED_FIELDS}
        assert counted["headers"].get("anthropic-beta") == sent["headers"].get("anthropic-beta")
    assert fake.count_requests[2]["body"]["tool_choice"] == {"type": "tool", "name": "Hypothesis"}
    # Pinned: a field Strands starts to send has to be sorted into the counted or uncounted ones on purpose.
    sent = {"max_tokens", "messages", "model", "stream", "system", "thinking", "tools"}
    assert [set(r["body"]) for r in fake.requests] == [sent, sent, sent | {"tool_choice"}]


def test_the_model_cannot_report_a_halt() -> None:
    fake = patched(PATCH, halted_by="step_limit")

    report = run(fake, reading())

    assert report.hypothesis.halted_by is None
    assert report.hypothesis.can_open_pr


# The runbook search, as the recorded pool-1 search showed it: db-pool-exhaustion.md and
# slow-database.md in full, the other five listed by name.
POOL_SEARCH = load(TOOL_FIXTURES / "runbooks_pool.json")["text"]


class Searching(Tools):
    """The scripted tools, and a runbook search that answers with text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def all(self) -> list:
        def search_runbooks(query: str) -> str:
            """Stands in for the runbook search.

            Args:
                query: What was measured.
            """
            self.runs.append("search_runbooks")
            return self.text

        return [*super().all(), search_runbooks]


def searching(text: str = POOL_SEARCH) -> Searching:
    return Searching(text)


def cites(runbook: str | None, *, search: bool = True) -> FakeAnthropic:
    replies = [Reply([tool_use("search_runbooks", {"query": "pool wait rose, then 500s"})])] if search else []
    return FakeAnthropic([*replies, answer({**DIAGNOSIS, "runbook_cited": runbook})])


def dropped_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.runner"]


def test_a_runbook_a_search_showed_in_full_is_kept(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.runner")
    for runbook in ("db-pool-exhaustion.md", "slow-database.md"):
        report = run(cites(runbook), searching())
        assert report.hypothesis.runbook_cited == runbook and report.citation_dropped is None
    assert dropped_events(caplog) == []


@pytest.mark.parametrize(
    "runbook, search, reason",
    [
        ("memory-leak.md", True, "only listed"),  # the search named it, and showed it no further
        ("db-pool-exhaustion.md", False, "never shown"),  # no search at all
        ("connection-pool.md", True, "never shown"),  # no such runbook
        ("DB-Pool-Exhaustion.md", True, "never shown"),  # a name only shown in other letters: exact or nothing
        ("db-pool-exhaustion.md: Database connection pool exhaustion", True, "never shown"),  # the title line
    ],
)
def test_a_runbook_not_shown_in_full_is_cleared_and_logged_never_resent(
    runbook: str, search: bool, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.runner")
    fake = cites(runbook, search=search)

    report = run(fake, searching())

    assert report.hypothesis.runbook_cited is None
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    assert report.citation_dropped == {"cited": runbook, "reason": reason}
    assert dropped_events(caplog) == [{"event": "citation_dropped", "cited": runbook, "reason": reason}]
    assert len(fake.requests) == (2 if search else 1), "nothing is sent back to the model"


def test_a_search_that_failed_shows_nothing() -> None:
    tools = searching("Could not read the runbooks: AWS returned ThrottlingException.")
    report = run(cites("db-pool-exhaustion.md"), tools)
    assert report.citation_dropped == {"cited": "db-pool-exhaustion.md", "reason": "never shown"}


def test_a_runbook_the_cap_cut_short_was_not_read_whole() -> None:
    body = "\n".join(f"- check {n} " + "x" * 90 for n in range(400))
    hits = [
        Hit(f"a--{s}.md", "a.md", section, 0.5, "A", f"# Runbook: A\n\n## {section}\n\n{body}")
        for s, section in enumerate(SHOWN_SECTIONS)
    ]
    text = render(hits)
    assert parse_results(text).partial == "a.md"

    report = run(cites("a.md"), searching(text))

    assert report.citation_dropped == {"cited": "a.md", "reason": "cut short"}


def test_the_check_reads_the_marked_results_the_model_was_given() -> None:
    """The run's sources keep each result as its tool wrote it, so the check reads the same text either way."""
    fake = cites("db-pool-exhaustion.md")
    report = run(fake, searching())
    results = [b["toolResult"] for m in report.messages for b in m["content"] if "toolResult" in b]
    (first,) = [r for r in results if "[source r1]" in r["content"][0]["text"]]
    assert POOL_SEARCH in first["content"][0]["text"]


# --- the patch: kept only if it passes its checks against a file this run read ----

# The read tool's result for 1.43.0's checkout/pricing.py, as the tool will render it.
PRICING_READ = code.render_file("1.43.0", "checkout/pricing.py", PRICING_1_43_0)


class Reading(Tools):
    """The scripted tools, and a code read that answers with each text in turn, the last one again after."""

    def __init__(self, *texts: str) -> None:
        super().__init__()
        self.texts = list(texts)

    def all(self) -> list:
        def read_repo_file(path: str) -> str:
            """Stands in for the code read.

            Args:
                path: The file.
            """
            self.runs.append("read_repo_file")
            return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

        return [*super().all(), read_repo_file]


def reading(*texts: str) -> Reading:
    return Reading(*(texts or (PRICING_READ,)))


def patched(remediation: dict[str, Any] | None, *, read: bool = True, **changes: Any) -> FakeAnthropic:
    """A run that reads the metrics, then the file unless read is False, then answers."""
    replies = [Reply([tool_use("get_metrics", {})])]
    if read:
        replies.append(Reply([tool_use("read_repo_file", {"path": "checkout/pricing.py"})]))
    return FakeAnthropic([*replies, answer({**DIAGNOSIS, "remediation": remediation, **changes})])


def test_a_patch_that_passes_is_kept_with_what_only_the_check_sets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.runner")

    report = run(patched(PATCH), reading())

    kept = report.hypothesis.remediation
    assert kept is not None and report.remediation_dropped is None
    assert (kept.source, kept.file_path, kept.rationale) == (PATCH["source"], PATCH["file_path"], PATCH["rationale"])
    assert (kept.version, kept.file_sha256) == ("1.43.0", code.digest(PRICING_1_43_0))
    assert kept.diff.startswith("--- a/services/checkout-api/checkout/pricing.py\n+++ b/services/checkout-api/checkout/pricing.py\n")
    assert "+    if promo is None:\n" in kept.diff
    assert report.hypothesis.can_open_pr
    assert dropped_events(caplog) == []
    # Made on 1.43.0, the patch gives 1.42.0 back: the null check, as it was.
    edits = [(e.old, e.new) for e in kept.edits]
    assert patches.apply_edits(kept.file_path, PRICING_1_43_0, edits).text == PRICING_1_42_0


def test_what_only_the_check_sets_is_never_the_models() -> None:
    # The strict schema leaves these out, so the API can't send them; if a model did,
    # the check writes its own over them.
    claimed = {**PATCH, "version": "1.42.0", "file_sha256": "0" * 64, "diff": "+ anything"}

    kept = run(patched(claimed), reading()).hypothesis.remediation

    assert (kept.version, kept.file_sha256) == ("1.43.0", code.digest(PRICING_1_43_0))
    assert kept.diff != "+ anything"


def test_an_answer_with_no_patch_needs_no_check_and_opens_nothing() -> None:
    report = run(patched(None), reading())
    assert report.hypothesis.remediation is None and report.remediation_dropped is None
    assert not report.hypothesis.can_open_pr


GONE = "x = 1\n"  # not in the file
IMPORT = {"old": '"""Order pricing."""\n', "new": '"""Order pricing."""\nimport os\n'}
BROKEN = {"old": PATCH["edits"][0]["old"], "new": "def apply_promo(:\n    return "}


@pytest.mark.parametrize(
    "remediation, read, reason",
    [
        ({**PATCH, "source": "c2"}, True, "no result c2 in this run"),  # one read, so c1 only
        (PATCH, False, "no result c1 in this run"),  # no read at all
        ({**PATCH, "source": "m1"}, True, "m1 isn't a read_repo_file result"),
        ({**PATCH, "file_path": "checkout/payments.py"}, True, "file_path isn't the file c1 shows"),
        ({**PATCH, "file_path": "./checkout/pricing.py"}, True, "file_path isn't the file c1 shows"),  # exact or nothing
        ({**PATCH, "rationale": "  "}, True, "no rationale"),
        ({**PATCH, "edits": []}, True, "no edits"),
        ({**PATCH, "edits": [{"old": GONE, "new": "x = 2\n"}]}, True, "edit 1: old text isn't in the file"),
        ({**PATCH, "edits": [{"old": "return ", "new": "return  "}]}, True, "edit 1: old text is in the file more than once"),
        ({**PATCH, "edits": [*PATCH["edits"], IMPORT]}, True, "adds an import"),
        ({**PATCH, "edits": [BROKEN]}, True, "doesn't parse"),
    ],
)
def test_a_patch_that_fails_a_check_is_cleared_and_logged_never_resent(
    remediation: dict[str, Any], read: bool, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.runner")
    fake = patched(remediation, read=read)

    report = run(fake, reading())

    assert report.hypothesis.remediation is None and not report.hypothesis.can_open_pr
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"], "the diagnosis stands"
    dropped = {"source": remediation["source"], "file_path": remediation["file_path"], "reason": reason}
    assert report.remediation_dropped == dropped
    assert dropped_events(caplog) == [{"event": "remediation_dropped", **dropped}]
    assert len(fake.requests) == (3 if read else 2), "nothing is sent back to the model"


@pytest.mark.parametrize(
    "text",
    [
        "\n".join(PRICING_READ.split("\n")[:5]) + "\n",  # cut short
        "Could not read checkout/handler.py: only files under checkout/ can be read.",  # a refusal
        PRICING_READ.replace("checkout/pricing.py at 1.43.0", "checkout/pricing.py at latest"),  # no release
    ],
)
def test_a_patch_against_a_read_that_isnt_one_whole_file_is_cleared(text: str) -> None:
    report = run(patched(PATCH), reading(text))
    assert report.hypothesis.remediation is None
    assert report.remediation_dropped["reason"] == "c1 isn't a read of one whole file"


def test_a_label_is_counted_per_kind_as_the_model_was_shown() -> None:
    # Two reads: the second is c2, whatever ran between them.
    replies = [
        Reply([tool_use("read_repo_file", {"path": "checkout/config.py"})]),
        Reply([tool_use("get_metrics", {})]),
        Reply([tool_use("read_repo_file", {"path": "checkout/pricing.py"})]),
    ]
    fake = FakeAnthropic([*replies, answer({**DIAGNOSIS, "remediation": {**PATCH, "source": "c2"}})])

    report = run(fake, reading("Could not read checkout/config.py.", PRICING_READ))

    assert report.hypothesis.remediation is not None and report.hypothesis.remediation.source == "c2"
    assert report.remediation_dropped is None


def test_a_failed_call_ends_the_run_and_is_charged_the_most_it_could_cost() -> None:
    fake = FakeAnthropic([])  # the fake answers 400: its script is empty

    report = run(fake)

    assert report.hypothesis is None
    assert report.failure.startswith("BadRequestError")
    assert report.model_calls == []
    counted = fake.count_requests[0]["bytes"]  # the fake's count, unscripted
    assert report.cost_usd == pytest.approx(HAIKU.most_a_call_costs(counted + count_margin(counted), 16_000))


# Strict tool use's own limits (platform docs, structured outputs: schema complexity limits).
STRICT_MAX_OPTIONAL = 24
STRICT_MAX_UNIONS = 16
STRICT_REJECTS = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def schema_nodes(node, path="$"):
    """Every subschema in a JSON schema, with its path; property names are never taken for keywords."""
    if isinstance(node, list):
        for i, sub in enumerate(node):
            yield from schema_nodes(sub, f"{path}[{i}]")
    elif isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                for name, sub in value.items():
                    yield from schema_nodes(sub, f"{path}.{key}.{name}")
            else:
                yield from schema_nodes(value, f"{path}.{key}")


def test_the_answer_tool_alone_goes_out_strict_on_every_call() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), Reply([text("Done.")], stop_reason="end_turn"), answer(DIAGNOSIS)])

    run(fake)

    for sent in fake.requests + fake.count_requests:
        strict = {tool["name"]: tool.get("strict") for tool in sent["body"]["tools"]}
        assert strict == {"get_metrics": None, "get_recent_logs": None, "Hypothesis": True}


def as_list(kind: Any) -> list[Any]:
    return kind if isinstance(kind, list) else [kind]


def test_the_strict_schema_is_one_the_api_takes() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    run(fake)

    schema = next(tool for tool in fake.requests[0]["body"]["tools"] if tool["name"] == "Hypothesis")["input_schema"]
    nodes = list(schema_nodes(schema))
    assert [(path, sorted(STRICT_REJECTS & set(node))) for path, node in nodes if STRICT_REJECTS & set(node)] == []
    # An object, or a nullable one: remediation goes out as type ["object", "null"], a type
    # array, which the API counts as a union; anyOf beside properties it would refuse.
    objects = [(path, node) for path, node in nodes if "object" in as_list(node.get("type"))]
    assert [path for path, node in objects] == [
        "$",
        "$.properties.evidence.items",
        "$.properties.inferences.items",
        "$.properties.remediation",
        "$.properties.remediation.properties.edits.items",
    ]
    assert not [path for path, node in nodes if "anyOf" in node and {"type", "properties"} & set(node)]
    assert all(node["additionalProperties"] is False for _, node in objects)
    # Every field required: with 14 optional ones the API refused the schema as too complex to compile.
    assert [(path, node["required"]) for path, node in objects] == [(path, list(node["properties"])) for path, node in objects]
    # The limits are over the whole request; this is its only strict tool.
    optional = sum(len(set(node["properties"]) - set(node.get("required", []))) for _, node in objects)
    unions = sum(
        "anyOf" in prop or isinstance(prop.get("type"), list)
        for _, node in objects
        for prop in node["properties"].values()
    )
    assert optional <= STRICT_MAX_OPTIONAL and unions <= STRICT_MAX_UNIONS
    # The fields the answer may leave unset go out nullable (a type array), each a union: 11 of
    # Evidence's and runbook_cited, plus remediation, an object or null (an anyOf). inferences and
    # not_checked go out as plain lists.
    assert (optional, unions) == (0, 13), "pinned: a field added or made nullable moves these on purpose"


def test_the_strict_schema_is_only_what_the_api_needs_changed() -> None:
    # What the model reads is the same: the fields and their descriptions. What changes is what strict
    # tool use needs: no rejected keyword, closed objects, every field required, and no null where
    # Pydantic takes none. (The tool's own description is Strands', untouched: format_request sets only
    # input_schema and strict.)
    from strands.tools.structured_output.structured_output_utils import convert_pydantic_to_tool_spec

    from sentinelops_core.models import Hypothesis

    fake = FakeAnthropic([answer(DIAGNOSIS)])

    run(fake)

    sent = next(tool for tool in fake.requests[0]["body"]["tools"] if tool["name"] == "Hypothesis")
    strands = convert_pydantic_to_tool_spec(Hypothesis)
    original = dict(schema_nodes(strands["inputSchema"]["json"]))
    no_null = {"$.properties.inferences", "$.properties.not_checked"}
    changed = {"additionalProperties", "required", "type"}
    for path, node in schema_nodes(sent["input_schema"]):
        dropped = set(original[path]) - set(node)
        added = set(node) - set(original[path])
        assert dropped <= STRICT_REJECTS, path
        assert added <= {"additionalProperties"}, path
        assert {k: v for k, v in node.items() if k not in changed and not isinstance(v, (dict, list))} == {
            k: v
            for k, v in original[path].items()
            if k not in STRICT_REJECTS | changed and not isinstance(v, (dict, list))
        }, path
        if path in no_null:
            assert (original[path]["type"], node["type"]) == (["array", "null"], "array"), path
        else:
            assert node.get("type") == original[path].get("type"), path
        if "required" in node:
            assert node["required"] == list(node["properties"]), path


def test_an_answer_written_out_in_full_is_the_answer_it_was() -> None:
    # Under strict tool use the answer writes every field: null where it used to leave one out, and []
    # for no inferences. Pydantic reads it as the same answer, accepted first time.
    from sentinelops_core.models import Evidence, Hypothesis

    full = {
        **DIAGNOSIS,
        "evidence": [{**dict.fromkeys(Evidence.model_fields), **one} for one in DIAGNOSIS["evidence"]],
        "inferences": [],
        "not_checked": [],
        "runbook_cited": None,
    }
    written_out, left_out = FakeAnthropic([answer(full)]), FakeAnthropic([answer(DIAGNOSIS)])

    assert run(written_out).hypothesis == run(left_out).hypothesis
    assert len(written_out.requests) == len(left_out.requests) == 1
    # Strands' schema would have let inferences be null, which Pydantic refuses: the strict schema doesn't.
    with pytest.raises(ValueError, match="valid list"):
        Hypothesis.model_validate({**DIAGNOSIS, "inferences": None})


def test_a_strict_answer_is_still_checked_for_what_the_schema_no_longer_says() -> None:
    # minLength and the 0-1 bound left the sent schema; Pydantic still holds the answer to them.
    fake = FakeAnthropic([answer({**DIAGNOSIS, "confidence": 1.5}), answer({**DIAGNOSIS, "root_cause": ""}), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.confidence == DIAGNOSIS["confidence"]
    rejected = [fake.tool_results(i) for i in (1, 2)]
    assert "less than or equal to 1" in " ".join(rejected[0])
    assert "at least 1 character" in " ".join(rejected[1])


LEAKED_TAIL = '</root_cause>\n<parameter name="confidence">0.9'


def guard_events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict]:
    return [
        e
        for e in (json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.guards")
        if e["event"] == event
    ]


def test_an_answer_whose_only_fault_is_a_leaked_tail_is_trimmed_not_sent_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")
    leaked = {**DIAGNOSIS, "root_cause": DIAGNOSIS["root_cause"] + LEAKED_TAIL}
    fake = FakeAnthropic([answer(leaked)])

    report = run(fake)

    assert len(fake.requests) == 1  # no resend
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    assert report.trims == [LEAKED_TAIL]
    assert [e["removed"] for e in guard_events(caplog, "answer_trimmed")] == [LEAKED_TAIL]
    assert guard_events(caplog, "answer_rejected") == []
    # The conversation keeps what the model wrote.
    written = [b["toolUse"]["input"] for m in report.messages for b in m["content"] if "toolUse" in b]
    assert written == [leaked]


def test_a_leaked_tail_with_another_fault_gets_the_normal_rejection(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")
    leaked = {**DIAGNOSIS, "root_cause": DIAGNOSIS["root_cause"] + LEAKED_TAIL, "confidence": 1.5}
    fake = FakeAnthropic([answer(leaked), answer(DIAGNOSIS)])

    report = run(fake)

    rejected = " ".join(fake.tool_results(1))
    assert "holds tool-call markup" in rejected and "less than or equal to 1" in rejected
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.trims == []
    assert len(guard_events(caplog, "answer_rejected")) == 1


def test_a_resend_ending_in_a_bare_closing_tag_is_trimmed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")
    bare = {**DIAGNOSIS, "root_cause": DIAGNOSIS["root_cause"] + "</root_cause>"}
    fake = FakeAnthropic([answer({**DIAGNOSIS, "confidence": 1.5}), answer(bare)])

    report = run(fake)

    assert len(fake.requests) == 2
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.trims == ["</root_cause>"]
    assert len(guard_events(caplog, "answer_rejected")) == 1


# --- a model that can't be forced to call a tool (Sonnet 5.5, Opus 5.5) -------------------
#
# Asked for its answer, such a model gets the request with tool_choice auto, never as
# native JSON, so its answer is a call to the strict tool like any other: trimmed,
# validated, and sent back at most twice. Text in place of that call is asked for again.

SONNET = price_for("anthropic", "claude-sonnet-5-5")
SONNET_SETTINGS = Settings(model_id="claude-sonnet-5-5")
FORCED = {"type": "tool", "name": "Hypothesis"}
AUTO = {"type": "auto"}
STRANDS_ASK = "You must format the previous response as structured output."
DONE = Reply([text("Done.")], stop_reason="end_turn")


@pytest.fixture
def unforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that hasn't yet seen the model refuse a forced tool: Strands keeps what it learns for the process."""
    for model_id in (SETTINGS.model_id, SONNET_SETTINGS.model_id):
        monkeypatch.setitem(_UNSUPPORTED_MECHANISMS, ("anthropic", model_id), set())


def cant_be_forced(replies: list) -> FakeAnthropic:
    return FakeAnthropic(replies, forces_tools=False)


def tool_choices(requests: list[dict]) -> list:
    return [r["body"].get("tool_choice") for r in requests]


def last_text(body: dict) -> str:
    """The text of a request's last message."""
    content = body["messages"][-1]["content"]
    return content if isinstance(content, str) else "".join(block.get("text", "") for block in content)


def test_the_ask_for_an_answer_given_as_text_is_these_words() -> None:
    # Pinned: the model reads it, so a change to it is made on purpose, as a change to the prompt is.
    assert ANSWER_AS_TEXT == (
        "No answer was recorded: your reply was text, and an answer is only taken from a call to the "
        "Hypothesis tool. Call the Hypothesis tool now with what you found."
    )
    assert Hypothesis.__name__ in ANSWER_AS_TEXT and ANSWER_AS_TEXT.isascii()


def test_a_model_that_cant_be_forced_is_asked_with_auto_and_answers_through_the_strict_tool(unforced: None) -> None:
    fake = cant_be_forced([Reply([tool_use("get_metrics")]), DONE, answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.answers_in_text == 0
    assert tool_choices(fake.requests) == [None, None, FORCED, AUTO]
    forced, again = fake.requests[2]["body"], fake.requests[3]["body"]
    # The request the API refused, sent again with only its tool_choice changed: no native JSON.
    assert "output_config" not in again
    assert {k: v for k, v in again.items() if k != "tool_choice"} == {k: v for k, v in forced.items() if k != "tool_choice"}
    assert last_text(again) == STRANDS_ASK  # in Strands' words, as a model that can be forced is asked
    (answer_tool,) = [tool for tool in again["tools"] if tool["name"] == "Hypothesis"]
    assert answer_tool["strict"] is True and len(again["tools"]) == 3  # every tool still offered
    # To the guard the refusal and the request sent again are one call, reserved once.
    assert len(report.model_calls) == 3


def test_a_model_that_can_be_forced_is_asked_as_before() -> None:
    fake = FakeAnthropic([DONE, answer(DIAGNOSIS)])

    report = run(fake)

    assert tool_choices(fake.requests) == [None, FORCED]
    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.answers_in_text == 0


def test_once_the_refusal_is_seen_the_request_goes_out_with_auto_and_is_counted_as_sent(unforced: None) -> None:
    fake = cant_be_forced([DONE, answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    # One refusal for the process: the resend of the malformed answer goes out with auto at once.
    assert tool_choices(fake.requests) == [None, FORCED, AUTO, AUTO]
    # The count of a forced request is refused as the request is, so that one call
    # is reserved on the byte bound; after it, each count is of the request sent.
    assert tool_choices(fake.count_requests) == [None, FORCED, AUTO]
    assert [call.counted_tokens is None for call in report.model_calls] == [False, True, False]
    sent = fake.requests[3]["body"]
    assert fake.count_requests[2]["body"] == {key: value for key, value in sent.items() if key in COUNTED_FIELDS}


def test_a_malformed_answer_from_a_model_that_cant_be_forced_is_sent_back_twice_then_the_run_halts(unforced: None) -> None:
    fake = cant_be_forced([DONE, answer(LEAKED), answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.halted_by is StopReason.RESEND_LIMIT
    assert "resend limit (2 resends of a malformed answer)" in report.hypothesis.not_checked[0]
    assert len(report.model_calls) == 4 and len(fake.replies) == 1, "no call after the third rejection"
    rejections = fake.tool_results(len(fake.requests) - 1)
    assert len(rejections) == 2 and all("holds tool-call markup ('</root_cause>')" in r for r in rejections)
    assert report.answers_in_text == 0


def test_a_leaked_tail_from_a_model_that_cant_be_forced_is_trimmed_not_sent_back(
    unforced: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")
    leaked = {**DIAGNOSIS, "root_cause": DIAGNOSIS["root_cause"] + LEAKED_TAIL}
    fake = cant_be_forced([DONE, answer(leaked)])

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.trims == [LEAKED_TAIL]
    assert guard_events(caplog, "answer_rejected") == [] and report.answers_in_text == 0
    assert len(report.model_calls) == 2  # no resend


def test_an_answer_in_text_is_asked_for_again_as_a_call_to_the_answer_tool(
    unforced: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops")
    said = Reply([text("The 1.43.0 deploy caused it.")], stop_reason="end_turn")
    fake = cant_be_forced([DONE, said, answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.hypothesis.halted_by is None
    assert report.answers_in_text == 1
    assert len(guard_events(caplog, "answer_rejected")) == 1  # counted as a resend
    runner_events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.runner"]
    assert runner_events == [{"event": "answer_in_text", "count": 1}]
    asked = fake.requests[-1]["body"]
    assert last_text(asked) == ANSWER_AS_TEXT and "tool_choice" not in asked
    # The conversation is only added to: what was sent before is the start of what's sent now.
    assert [m["role"] for m in asked["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    assert asked["messages"][:3] == fake.requests[2]["body"]["messages"]
    assert [b["text"] for m in report.messages if m["role"] == "assistant" for b in m["content"] if "text" in b] == [
        "Done.",
        "The 1.43.0 deploy caused it.",
    ]


def test_a_whole_answer_written_out_as_text_is_not_taken(unforced: None) -> None:
    # Strands would take text that is one JSON object and validates. It didn't come
    # through the strict tool, and nothing trims it, so it's asked for again.
    written = Reply([text(json.dumps(DIAGNOSIS))], stop_reason="end_turn")
    fake = cant_be_forced([DONE, written, answer({**DIAGNOSIS, "confidence": 0.8})])

    report = run(fake)

    assert report.answers_in_text == 1 and last_text(fake.requests[-1]["body"]) == ANSWER_AS_TEXT
    assert report.hypothesis.confidence == 0.8  # the tool call's answer, not the text's


def test_answers_in_text_count_toward_the_resend_cap(unforced: None) -> None:
    # Each ask gets text, and then text again when Strands asks in its own words: two calls a refusal.
    fake = cant_be_forced([DONE] * 6 + [answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.halted_by is StopReason.RESEND_LIMIT and report.answers_in_text == 3
    assert "resend limit (2 resends of a malformed answer)" in report.hypothesis.not_checked[0]
    assert len(report.model_calls) == 6 and len(fake.replies) == 1, "no call after the third refusal"
    assert [last_text(r["body"]) for r in fake.requests].count(ANSWER_AS_TEXT) == 2
    assert not report.hypothesis.can_open_pr


def test_text_and_malformed_answers_share_the_one_cap(unforced: None) -> None:
    fake = cant_be_forced([DONE, DONE, answer(LEAKED), answer(LEAKED), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.halted_by is StopReason.RESEND_LIMIT
    assert report.answers_in_text == 1 and len(fake.replies) == 1


def test_a_sonnet_run_is_refused_under_the_production_cap_and_finishes_under_its_arms(unforced: None) -> None:
    def script() -> FakeAnthropic:
        usage = {"input_tokens": 5_000, "output_tokens": 300}
        return cant_be_forced(
            [
                Reply([tool_use("get_metrics")], **usage),
                Reply([text("Done.")], stop_reason="end_turn", **usage),
                Reply([tool_use("Hypothesis", DIAGNOSIS)], **usage),
            ]
        )

    fake = script()
    refused = run(fake, settings=SONNET_SETTINGS, price=SONNET)

    # 16,000 output tokens at $10 a million is $0.16 before any prompt: past $0.10, so nothing is sent.
    assert refused.hypothesis.halted_by is StopReason.COST_LIMIT and fake.requests == []

    arm = ARMS["sonnet"]
    fake = script()
    report = run(fake, settings=SONNET_SETTINGS, price=SONNET, limits=Limits(max_cost_usd=arm.per_run_usd))

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.hypothesis.halted_by is None
    assert {r["body"]["model"] for r in fake.requests} == {arm.model_id}
    assert tool_choices(fake.requests) == [None, None, FORCED, AUTO]
    # 3 calls of 5,000 tokens in at $2 and 300 out at $10, per million
    assert report.cost_usd == pytest.approx(3 * (0.010 + 0.003))
    assert all(call.reserved_usd > 0.16 for call in report.model_calls)


# --- the tool calls a run makes after it's asked for its answer -----------------------------
#
# A run that ends its investigation in text is asked for its answer. A model that takes a
# forced tool can then only answer; one asked with tool_choice auto can go on investigating.


def test_a_run_that_answers_unasked_records_no_ask() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.tool_calls == ["get_metrics"] and report.tool_calls_after_ask is None


def test_a_forced_model_makes_no_tool_call_after_it_is_asked() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), DONE, answer(DIAGNOSIS)])

    report = run(fake)

    # Asked, so not None; the call before the ask isn't counted, and a forced model can make none after.
    assert report.tool_calls == ["get_metrics"] and report.tool_calls_after_ask == []


def test_the_tool_calls_a_model_asked_with_auto_goes_on_to_make_are_recorded(
    unforced: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.model")
    fake = cant_be_forced(
        [
            Reply([tool_use("get_metrics")]),
            DONE,
            Reply([tool_use("get_recent_logs")]),
            Reply([tool_use("get_metrics")]),
            answer(DIAGNOSIS),
        ]
    )

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"] and report.answers_in_text == 0
    assert report.tool_calls == ["get_metrics", "get_recent_logs", "get_metrics"]
    assert report.tool_calls_after_ask == ["get_recent_logs", "get_metrics"]
    # Every call after the first ask still asks for the answer, with every tool offered.
    assert tool_choices(fake.requests) == [None, None, FORCED, AUTO, AUTO, AUTO]
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.model"]
    assert [e["tool_calls"] for e in events if e["event"] == "asked_for_answer"] == [1]  # logged once, at the first ask


def test_a_tool_call_made_after_the_ask_again_counts_too(unforced: None) -> None:
    said = Reply([text("The 1.43.0 deploy caused it.")], stop_reason="end_turn")
    fake = cant_be_forced([DONE, said, Reply([tool_use("get_recent_logs")]), answer(DIAGNOSIS)])

    report = run(fake)

    assert report.hypothesis.root_cause == DIAGNOSIS["root_cause"]
    assert report.answers_in_text == 1 and report.tool_calls_after_ask == ["get_recent_logs"]
