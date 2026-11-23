"""search_runbooks on recorded Retrieve responses, and its parser.

The fixtures (runbooks_*.json) are three of the retrieval set's queries,
searched through the tool against the live knowledge base on 2026-11-23, the
bucket and IDs masked: pool-1 (its runbook first), leak-1 (memory-leak.md 5th,
so only listed), and none-2 (no runbook fits). The Stubber fails a test if the
tool asks for anything else.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import RECORDED_KB, Stubbed, load

from sentinelops_tools.env import ToolEnv
from sentinelops_tools.runbooks import (
    CUT_MARK,
    MAX_QUERY_CHARS,
    MAX_RESULT_CHARS,
    NOT_RETURNED,
    SHOWN_SECTIONS,
    Hit,
    _body,
    hits_from_retrieve,
    parse_results,
    rank,
    render,
    search,
)

FIXTURES = ["runbooks_pool", "runbooks_leak", "runbooks_none"]
RUNBOOKS = {
    "bad-deploy.md",
    "db-pool-exhaustion.md",
    "downstream-timeout.md",
    "lambda-throttling.md",
    "memory-leak.md",
    "slow-database.md",
    "traffic-surge.md",
}
NOW = datetime(2026, 11, 23, tzinfo=UTC)


def request(query: str) -> dict[str, Any]:
    return {
        "knowledgeBaseId": RECORDED_KB,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 100}},
    }


def recorded_query(name: str) -> str:
    return load(name)["calls"][0]["params"]["retrievalQuery"]["text"]


def recorded_hits(name: str) -> list[Hit]:
    return hits_from_retrieve(load(name)["calls"][0]["response"])


# The search


@pytest.mark.parametrize("name", FIXTURES)
def test_one_retrieve_returns_every_section(name: str) -> None:
    fixture = load(name)
    (call,) = fixture["calls"]
    assert call["params"] == request(recorded_query(name))
    hits = recorded_hits(name)
    assert len(hits) == 35 and len({h.document for h in hits}) == 35
    assert {h.runbook for h in hits} == RUNBOOKS


@pytest.mark.parametrize("name", FIXTURES)
def test_the_two_closest_are_shown_in_full_and_the_rest_listed(name: str) -> None:
    hits = recorded_hits(name)
    page = parse_results(load(name)["text"])

    assert page is not None and not page.cut
    assert page.shown == rank(hits)[:2]
    assert page.listed == rank(hits)[2:]
    assert set(page.shown) | set(page.listed) == RUNBOOKS
    for runbook in page.shown:
        assert list(page.sections[runbook]) == list(SHOWN_SECTIONS)
        for section, text in page.sections[runbook].items():
            (hit,) = [h for h in hits if h.runbook == runbook and h.section == section]
            assert text == _body(hit)
            assert text and "# Runbook:" not in text
    assert page.titles == {h.runbook: h.title for h in hits}


@pytest.mark.parametrize("name", FIXTURES)
def test_fixes_are_never_shown_and_no_score_is(name: str) -> None:
    text = load(name)["text"]
    assert "## Resolution" not in text and "## Prevention" not in text
    assert all(f"{h.score:.3f}" not in text for h in recorded_hits(name))


def test_the_retrieval_sets_misses_show_as_listed_only() -> None:
    page = parse_results(load("runbooks_leak")["text"])
    assert page is not None
    assert page.shown == ["traffic-surge.md", "downstream-timeout.md"]
    assert "memory-leak.md" in page.listed


def test_the_query_is_sent_on_one_line() -> None:
    stubbed = Stubbed(NOW)
    fixture = load("runbooks_pool")
    query = recorded_query("runbooks_pool")
    stubbed.expect("bedrock-agent-runtime", "retrieve", request(query), fixture["calls"][0]["response"])
    with stubbed:
        text = search(stubbed.env, "  " + query.replace(" ", "\n  ", 3) + "\t")
    assert text == fixture["text"]


def test_a_long_query_is_cut_and_says_so() -> None:
    stubbed = Stubbed(NOW)
    query = "pool wait rose " * 50
    cut = " ".join(query.split())[:MAX_QUERY_CHARS]
    stubbed.expect("bedrock-agent-runtime", "retrieve", request(cut), load("runbooks_pool")["calls"][0]["response"])
    with stubbed:
        text = search(stubbed.env, query)
    assert text.split("\n")[1] == f"The query was cut to its first {MAX_QUERY_CHARS} characters."
    assert parse_results(text) is not None


def test_an_empty_query_searches_nothing() -> None:
    with Stubbed(NOW) as stubbed:
        assert search(stubbed.env, " \n ") == "Give search_runbooks a query: what you measured."


def test_without_a_knowledge_base_it_says_so_and_searches_nothing() -> None:
    env = ToolEnv(logs=None, cloudwatch=None, dynamodb=None)
    assert search(env, "pool wait") == "Could not search the runbooks: no knowledge base is set up for this run."
    assert parse_results(search(env, "pool wait")) is None


def test_an_aws_error_reaches_the_model_as_its_code_only() -> None:
    stubbed = Stubbed(NOW)
    stubbed.stubbers["bedrock-agent-runtime"].add_client_error(
        "retrieve", "AccessDeniedException", "User: arn:aws:sts::000011112222:assumed-role/x is not authorized"
    )
    with stubbed:
        text = search(stubbed.env, "pool wait")
    assert text == "Could not read the runbooks: AWS returned AccessDeniedException."


def test_a_search_that_finds_nothing_says_so() -> None:
    stubbed = Stubbed(NOW)
    stubbed.expect("bedrock-agent-runtime", "retrieve", request("pool wait"), {"retrievalResults": []})
    with stubbed:
        text = search(stubbed.env, "pool wait")
    assert text == "The search found no runbooks."
    assert parse_results(text) is None


# The renderer's edges, on made-up results


def hit(runbook: str, section: str, body: str = "text") -> Hit:
    title = runbook[:-3].replace("-", " ").title()
    text = f"# Runbook: {title}\n\n## {section}\n\n{body}"
    return Hit(f"{runbook[:-3]}--{section.lower().replace(' ', '-')}.md", runbook, section, 0.5, title, text)


def test_a_section_the_search_missed_is_named_as_missing() -> None:
    text = render([hit("a.md", "Symptoms"), hit("a.md", "Diagnosis"), hit("b.md", "Symptoms")])
    page = parse_results(text)
    assert page is not None
    assert page.sections["a.md"]["What this is not"] == NOT_RETURNED
    assert page.sections["b.md"] == {"Symptoms": "text", "What this is not": NOT_RETURNED, "Diagnosis": NOT_RETURNED}
    assert page.listed == []


def test_a_runbook_with_no_ranked_section_is_still_listed() -> None:
    page = parse_results(
        render([hit("a.md", "Symptoms"), hit("b.md", "Diagnosis"), hit("c.md", "Resolution"), hit("d.md", "Symptoms")])
    )
    assert page is not None
    assert (page.shown, page.listed) == (["a.md", "b.md"], ["d.md", "c.md"])


def test_one_runbook_alone_is_shown_and_none_listed() -> None:
    text = render([hit("a.md", "Symptoms")])
    assert text.startswith("The 1 runbooks closest") and text.endswith("=== Not shown, further from the query ===\n(none)")
    page = parse_results(text)
    assert page is not None and (page.shown, page.listed) == (["a.md"], [])


def test_a_result_past_the_cap_is_cut_and_says_so() -> None:
    big = "\n".join(f"- line {n} " + "x" * 90 for n in range(400))
    text = render([hit("a.md", s, big) for s in SHOWN_SECTIONS] + [hit("b.md", "Symptoms")])
    assert len(text) <= MAX_RESULT_CHARS + 100
    assert text.split("\n")[-1] == f"{CUT_MARK} The result passed {MAX_RESULT_CHARS:,} characters."
    page = parse_results(text)
    assert page is not None and page.cut and page.shown == ["a.md"]


def test_a_body_keeps_its_lists_and_bold_lines() -> None:
    body = "**Early**\n\n- one\n- two: `x`\n\nLast line."
    page = parse_results(render([hit("a.md", "Symptoms", body)]))
    assert page is not None and page.sections["a.md"]["Symptoms"] == body


def test_text_that_is_not_a_search_result_parses_to_none() -> None:
    assert parse_results("") is None
    assert parse_results("Could not read the runbooks: AWS returned ThrottlingException.") is None


# The session


class Session:
    """Stands in for boto3.Session, noting each client asked for, as a replay's window session would refuse one."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def client(self, service: str, **_: Any) -> object:
        self.made.append(service)
        return object()


def test_the_retrieval_client_is_made_only_with_a_knowledge_base() -> None:
    without, with_kb = Session(), Session()
    assert ToolEnv.from_session(without).bedrock_agent_runtime is None
    env = ToolEnv.from_session(with_kb, knowledge_base_id="KB1")
    assert "bedrock-agent-runtime" not in without.made
    assert with_kb.made[-1] == "bedrock-agent-runtime" and env.knowledge_base_id == "KB1"
