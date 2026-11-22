"""The runbooks' format, the leak scan, and the documents the knowledge base indexes.

The format is what the builder (build_runbooks.py) splits on: a title, a
preamble, and five sections in order, ASCII only, each section short enough to
be one chunk, every runbook it names present. The leak scan keeps out what
only the demo's staging knows: the answer must come from the data the agent
reads, never from a runbook that names it.
"""

import json
import re
from pathlib import Path

import pytest

import build_runbooks as rb

GOOD = """# Runbook: Example

**Service:** checkout-api
**Severity:** SEV-2
**Owner:** this service's team.

## Symptoms

Something rises (see `other.md`).

## What this is not

Not that.

## Diagnosis

1. Look.

## Resolution

Fix it.

## Prevention

- Alarm on it
"""

# Only the demo's staging knows these, and each would hand the agent an answer
# or a suspect: the releases and commits in the deploy history, the authors of
# its records, the harmless config changes logged beside each break, the
# code a bad release changes, and how the demo stages a break. "promo" isn't
# here: requests log `has_promo`, a field the agent reads anyway.
STAGING = [
    r"1\.42",
    r"1\.43",
    r"3f8a2d1",
    r"9c41e7b",
    r"\btomas\b",
    r"\bmaya\b",
    r"\bpriya\b",
    r"\bken\b",
    r"\bsimplify",
    r"CART_TTL_HOURS",
    r"PRODUCT_CACHE_TTL_S",
    r"LOG_SAMPLE_RATE",
    r"RECOMMENDATIONS_ENABLED",
    r"apply_promo",
    r"pricing\.py",
    r"null check",
    r"\bburst",
    r"\bdecoy",
    r"\binject",
    r"\bflags?\b",
    r"fault controller",
    r"traffic generator",
    r"\bsim/",
]


def write_runbooks(folder: Path, runbooks: dict[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in runbooks.items():
        (folder / name).write_bytes(text.encode("utf-8"))
    return folder


def problems(text: str, name: str = "example.md") -> list[str]:
    _, found = rb.parse(name, text, known={name, "other.md"})
    return found


# The real runbooks


def test_every_runbook_has_the_format() -> None:
    corpus = rb.load()  # raises FormatError, listing every problem, if one breaks it

    assert len(corpus.runbooks) == 7
    assert len(corpus.sections) == 7 * len(rb.SECTIONS)
    for runbook in corpus.runbooks:
        assert list(runbook.sections) == list(rb.SECTIONS)


def test_no_runbook_names_what_only_the_staging_knows() -> None:
    hits = []
    for path in sorted(rb.RUNBOOKS.glob("*.md")):
        text = rb.read_text(path)
        for pattern in STAGING:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.name}:{line}: {match.group(0)!r}")
    assert hits == []


def test_the_build_writes_one_document_and_one_metadata_file_per_section(tmp_path) -> None:
    corpus = rb.load()
    out = tmp_path / "runbook-sections"
    manifest = rb.write(corpus, out)

    documents = sorted(path.name for path in out.glob("*.md"))
    assert documents == sorted(section.file for section in corpus.sections)
    assert len(list(out.glob("*.metadata.json"))) == len(documents)

    summary = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest.parent == tmp_path
    assert summary == {
        "corpus": corpus.digest,
        "runbooks": 7,
        "sections": 35,
        "documents": documents,
    }

    meta = json.loads((out / "bad-deploy--what-this-is-not.md.metadata.json").read_text(encoding="utf-8"))
    assert meta == {
        "metadataAttributes": {
            "runbook": "bad-deploy.md",
            "title": "Bad deploy (a release that fails requests)",
            "section": "What this is not",
            "corpus": corpus.digest,
        }
    }
    text = (out / "bad-deploy--what-this-is-not.md").read_bytes()
    assert text.startswith(b"# Runbook: Bad deploy (a release that fails requests)\n\n## What this is not\n\n")
    assert b"\r" not in text
    assert b"## Symptoms" not in text  # one section only


# The format, on a runbook made to break it


def test_a_runbook_in_the_format_passes() -> None:
    assert problems(GOOD) == []
    runbook, _ = rb.parse("example.md", GOOD, known={"example.md", "other.md"})
    assert runbook.title == "Example"
    assert runbook.sections["Symptoms"] == "Something rises (see `other.md`)."


@pytest.mark.parametrize(
    "change, expected",
    [
        (("# Runbook: Example", "# Example"), "line 1: the title must start with '# Runbook: '"),
        (("Not that.", "Not that \u2014 at all."), "line 13: not ASCII (U+2014)"),
        (("## Diagnosis", "## Diagnose"), "the section headers must be"),
        (("## Prevention\n\n- Alarm on it\n", "## Prevention\n\n- Alarm on it\n\n## Notes\n\nMore.\n"), "found Symptoms"),
        (("**Owner:** this service's team.\n", ""), "the preamble has no **Owner:** line"),
        (("Fix it.", ""), "section 'Resolution' is empty"),
        (("see `other.md`", "see `missing.md`"), "links missing.md, which isn't in runbooks/"),
        (("1. Look.", "# Look."), "a second title"),
    ],
)
def test_a_runbook_that_breaks_the_format_is_refused(change, expected) -> None:
    old, new = change
    assert old in GOOD
    found = problems(GOOD.replace(old, new, 1))
    assert any(expected in problem for problem in found), found


def test_the_two_runbook_headers_swapped_are_refused() -> None:
    swapped = GOOD.replace("## Resolution", "## TEMP").replace("## Diagnosis", "## Resolution").replace("## TEMP", "## Diagnosis")
    assert any("in that order" in problem for problem in problems(swapped))


def test_a_section_over_the_limit_is_refused() -> None:
    long = GOOD.replace("Fix it.", "x" * (rb.MAX_SECTION_CHARS + 1))
    assert any("the limit is 4,000" in problem for problem in problems(long))
    at_limit = GOOD.replace("Fix it.", "x" * rb.MAX_SECTION_CHARS)
    assert problems(at_limit) == []


def test_load_lists_every_problem_in_every_runbook(tmp_path) -> None:
    folder = write_runbooks(
        tmp_path / "runbooks",
        {
            "other.md": GOOD.replace("see `other.md`", "see `example.md`"),
            "example.md": GOOD.replace("## Diagnosis", "## Diagnose").replace("Fix it.", ""),
        },
    )
    with pytest.raises(rb.FormatError) as refused:
        rb.load(folder)
    message = str(refused.value)
    assert "example.md: the section headers must be" in message
    assert "example.md: line" in message and "'Resolution' is empty" in message
    assert "other.md" not in message


# The digest


def test_the_digest_ignores_line_endings(tmp_path) -> None:
    runbooks = {"example.md": GOOD, "other.md": GOOD}
    lf = rb.load(write_runbooks(tmp_path / "lf", runbooks))
    crlf = rb.load(write_runbooks(tmp_path / "crlf", {k: v.replace("\n", "\r\n") for k, v in runbooks.items()}))
    assert lf.digest == crlf.digest
    assert re.fullmatch(r"[0-9a-f]{12}", lf.digest)


def test_the_digest_changes_with_a_section_or_a_title(tmp_path) -> None:
    base = rb.load(write_runbooks(tmp_path / "a", {"example.md": GOOD, "other.md": GOOD})).digest
    edited = rb.load(write_runbooks(tmp_path / "b", {"example.md": GOOD.replace("Fix it.", "Fix it now."), "other.md": GOOD}))
    renamed = rb.load(write_runbooks(tmp_path / "c", {"example.md": GOOD, "other.md": GOOD.replace("# Runbook: Example", "# Runbook: Other")}))
    assert edited.digest != base
    assert renamed.digest != base


def test_the_digest_ignores_the_preamble(tmp_path) -> None:
    """The preamble is never indexed, so changing it changes no document."""
    base = rb.load(write_runbooks(tmp_path / "a", {"example.md": GOOD, "other.md": GOOD})).digest
    severity = GOOD.replace("SEV-2", "SEV-3")
    assert rb.load(write_runbooks(tmp_path / "b", {"example.md": severity, "other.md": GOOD})).digest == base


# Writing


def test_a_rebuild_replaces_the_earlier_build(tmp_path) -> None:
    out = tmp_path / "runbook-sections"
    out.mkdir()
    (out / "gone--symptoms.md").write_text("old", encoding="utf-8")
    (out / "gone--symptoms.md.metadata.json").write_text("{}", encoding="utf-8")
    rb.write(rb.load(), out)
    assert not (out / "gone--symptoms.md").exists()
    assert not (out / "gone--symptoms.md.metadata.json").exists()


def test_the_build_refuses_a_folder_holding_anything_else(tmp_path) -> None:
    out = tmp_path / "somewhere"
    out.mkdir()
    (out / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(rb.FormatError, match="notes.txt"):
        rb.write(rb.load(), out)
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_main_reports_the_build_and_fails_on_a_broken_runbook(tmp_path, capsys) -> None:
    good = write_runbooks(tmp_path / "good", {"example.md": GOOD, "other.md": GOOD})
    assert rb.main(["--runbooks", str(good), "--out", str(tmp_path / "out")]) == 0
    assert "10 sections from 2 runbooks, corpus " in capsys.readouterr().out

    broken = write_runbooks(tmp_path / "broken", {"example.md": GOOD.replace("## Diagnosis", "## Diagnose"), "other.md": GOOD})
    assert rb.main(["--runbooks", str(broken), "--out", str(tmp_path / "out2")]) == 1
    assert "example.md: the section headers must be" in capsys.readouterr().err
    assert not (tmp_path / "out2").exists()
