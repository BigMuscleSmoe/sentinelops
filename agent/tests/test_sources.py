"""Tool results as citable sources: each result labeled, log lines numbered, and both read back off."""

import asyncio
import inspect

import pytest

from sources import PREFIXES, Sources, cited, number_lines, unmark
from stubs import TOOL_FIXTURES, load

FIXTURES = {
    "get_metrics": "metrics_bad_deploy",
    "get_recent_logs": "logs_bad_deploy_errors",
    "get_deploy_history": "history_bad_deploy",
}


def test_each_kind_of_result_counts_its_own_labels() -> None:
    sources = Sources()
    labels = [
        unmark(sources.mark(tool, "x"))[0]
        for tool in ["get_metrics", "get_recent_logs", "get_metrics", "get_deploy_history", "get_recent_logs"]
    ]
    assert labels == ["m1", "l1", "m2", "d1", "l2"]


def test_only_log_lines_are_numbered_from_one() -> None:
    text = "3 checkout-api error lines\nLogs can trail.\n{\"a\": 1}\n{\"a\": 2}\nnote\n{\"a\": 3}"
    assert number_lines(text) == "3 checkout-api error lines\nLogs can trail.\n[1] {\"a\": 1}\n[2] {\"a\": 2}\nnote\n[3] {\"a\": 3}"
    marked = Sources().mark("get_metrics", text)
    assert "[1] " not in marked  # a metrics table is cited by row time, never by number


@pytest.mark.parametrize("tool", sorted(FIXTURES))
def test_marks_come_off_to_the_tools_exact_text(tool: str) -> None:
    text = load(TOOL_FIXTURES / f"{FIXTURES[tool]}.json")["text"]
    label, plain, numbers = unmark(Sources().mark(tool, text))
    assert label == f"{PREFIXES[tool]}1"
    assert plain == text
    log_lines = sum(line.startswith("{") for line in text.split("\n"))
    assert numbers == ({n: n - 1 for n in range(1, log_lines + 1)} if tool == "get_recent_logs" else {})


def test_a_result_with_no_label_reads_as_unlabeled() -> None:
    assert unmark("checkout-api metrics, 60 s periods")[0] is None


def test_a_tool_with_no_prefix_is_refused_when_wrapped() -> None:
    def read_repo_file(path: str) -> str:
        return path

    with pytest.raises(ValueError, match="read_repo_file has no source prefix"):
        cited(Sources(), read_repo_file)


def test_the_wrapper_keeps_the_signature_and_docstring_the_model_is_shown() -> None:
    def get_metrics(minutes: int = 10, as_of: str | None = None) -> str:
        """Read the metrics."""
        return f"{minutes}"

    wrapped = cited(Sources(), get_metrics)
    assert wrapped.__name__ == "get_metrics" and wrapped.__doc__ == "Read the metrics."
    assert inspect.signature(wrapped) == inspect.signature(get_metrics)
    assert wrapped(minutes=5) == "[source m1]\n5"


def test_an_async_tool_is_labeled_when_it_returns() -> None:
    async def get_recent_logs() -> str:
        return "{\"a\": 1}"

    wrapped = cited(Sources(), get_recent_logs)
    assert inspect.iscoroutinefunction(wrapped)
    assert asyncio.run(wrapped()) == "[source l1]\n[1] {\"a\": 1}"


def test_a_call_that_raises_takes_no_label() -> None:
    calls = []

    def get_deploy_history() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return "ok"

    wrapped = cited(Sources(), get_deploy_history)
    with pytest.raises(RuntimeError):
        wrapped()
    assert wrapped() == "[source d1]\nok"


def test_each_result_is_kept_as_the_tool_wrote_it_in_order() -> None:
    """The run's citation check reads its searches here (runner.check_citation)."""
    sources = Sources()
    sources.mark("search_runbooks", "first search")
    sources.mark("get_metrics", "a table")
    sources.mark("search_runbooks", "second search")
    assert sources.results == [("search_runbooks", "first search"), ("get_metrics", "a table"), ("search_runbooks", "second search")]
