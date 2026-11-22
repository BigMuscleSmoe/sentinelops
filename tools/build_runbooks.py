"""Split the runbooks into the knowledge base's documents: one file per section.

Each runbook in runbooks/ has a title, a short preamble (service, severity,
owner), and five sections under these headers, in this order: Symptoms, What
this is not, Diagnosis, Resolution, Prevention. Each section becomes one
document, holding the runbook's title line, the section's header, and its
text, beside a metadata file that Bedrock reads with it: the runbook's file
name, its title, the section, and the corpus digest.

The knowledge base chunks nothing itself, so a chunk is exactly one section.
The digest covers every section file's name and text, with line endings made
LF first, so a Windows checkout and CI build the same corpus. A sweep compares
it with what the live knowledge base returns, and refuses a stale one.

Stdlib only, and run before a plan (python tools/build_runbooks.py), like
agent/build_runtime.py: it never ships in a Lambda or the Runtime. A runbook
that breaks the format fails the build, with every problem listed.
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNBOOKS = REPO / "runbooks"
OUT = REPO / "infra" / ".build" / "runbook-sections"

SECTIONS = ("Symptoms", "What this is not", "Diagnosis", "Resolution", "Prevention")
PREAMBLE = ("**Service:**", "**Severity:**", "**Owner:**")
TITLE_PREFIX = "# Runbook: "
# Titan Text Embeddings V2 reads up to 8,192 tokens; a section stays far below
# it, and short enough to show the agent three of them for two runbooks.
MAX_SECTION_CHARS = 4000
DIGEST_CHARS = 12
# A runbook named in backticks, as in "(see `bad-deploy.md`)".
LINK = re.compile(r"`([a-z0-9-]+\.md)`")
METADATA_SUFFIX = ".metadata.json"


class FormatError(Exception):
    """A runbook that breaks the format. Its message lists every problem found."""


@dataclass(frozen=True)
class Runbook:
    file: str  # "bad-deploy.md"
    title: str  # the title line, less "# Runbook: "
    sections: dict[str, str]  # each header's text, stripped, in SECTIONS order


@dataclass(frozen=True)
class Section:
    runbook: Runbook
    name: str

    @property
    def file(self) -> str:
        """The document's name: the runbook's stem and the section, as "bad-deploy--symptoms.md"."""
        slug = self.name.lower().replace(" ", "-")
        return f"{Path(self.runbook.file).stem}--{slug}.md"

    @property
    def text(self) -> str:
        return f"{TITLE_PREFIX}{self.runbook.title}\n\n## {self.name}\n\n{self.runbook.sections[self.name]}\n"


@dataclass(frozen=True)
class Corpus:
    runbooks: tuple[Runbook, ...]
    sections: tuple[Section, ...]
    digest: str


def read_text(path: Path) -> str:
    """A runbook's text with LF line endings: a checkout here may be CRLF."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def parse(file: str, text: str, known: set[str]) -> tuple[Runbook | None, list[str]]:
    """One runbook and the problems found in it. `known` is every runbook's file name, for links."""
    problems: list[str] = []
    lines = text.split("\n")
    for number, line in enumerate(lines, start=1):
        bad = sorted({char for char in line if ord(char) > 127})
        if bad:
            shown = ", ".join(f"U+{ord(char):04X}" for char in bad)
            problems.append(f"line {number}: not ASCII ({shown})")

    if not lines or not lines[0].startswith(TITLE_PREFIX) or not lines[0][len(TITLE_PREFIX) :].strip():
        problems.append(f"line 1: the title must start with {TITLE_PREFIX!r}")
    title = lines[0][len(TITLE_PREFIX) :].strip() if lines else ""

    headers = [(number, line[3:].strip()) for number, line in enumerate(lines, start=1) if line.startswith("## ")]
    for number, line in enumerate(lines[1:], start=2):
        if line.startswith("# "):
            problems.append(f"line {number}: a second title")
    if [name for _, name in headers] != list(SECTIONS):
        found = ", ".join(name for _, name in headers) or "none"
        problems.append(f"the section headers must be {', '.join(SECTIONS)}, in that order; found {found}")

    first_header = headers[0][0] if headers else len(lines) + 1
    preamble = lines[1 : first_header - 1]
    for label in PREAMBLE:
        if not any(line.startswith(label) for line in preamble):
            problems.append(f"the preamble has no {label} line")

    sections: dict[str, str] = {}
    for index, (number, name) in enumerate(headers):
        end = headers[index + 1][0] - 1 if index + 1 < len(headers) else len(lines)
        body = "\n".join(lines[number:end]).strip()
        if not body:
            problems.append(f"line {number}: section {name!r} is empty")
        elif len(body) > MAX_SECTION_CHARS:
            problems.append(f"line {number}: section {name!r} is {len(body):,} characters; the limit is {MAX_SECTION_CHARS:,}")
        sections[name] = body

    for linked in sorted(set(LINK.findall(text)) - known):
        problems.append(f"links {linked}, which isn't in runbooks/")

    if problems:
        return None, problems
    return Runbook(file=file, title=title, sections={name: sections[name] for name in SECTIONS}), []


def load(runbooks_dir: Path = RUNBOOKS) -> Corpus:
    """Every runbook, split and checked. Raises FormatError listing every problem in every runbook."""
    paths = sorted(runbooks_dir.glob("*.md"))
    if not paths:
        raise FormatError(f"no runbooks in {runbooks_dir}")
    known = {path.name for path in paths}
    runbooks: list[Runbook] = []
    problems: list[str] = []
    for path in paths:
        runbook, found = parse(path.name, read_text(path), known)
        problems.extend(f"{path.name}: {problem}" for problem in found)
        if runbook is not None:
            runbooks.append(runbook)
    if problems:
        raise FormatError("\n".join(problems))
    sections = tuple(Section(runbook, name) for runbook in runbooks for name in SECTIONS)
    return Corpus(runbooks=tuple(runbooks), sections=sections, digest=digest(sections))


def digest(sections: tuple[Section, ...]) -> str:
    """The first DIGEST_CHARS hex characters of SHA-256 over every section file's name and text, by name."""
    hasher = hashlib.sha256()
    for section in sorted(sections, key=lambda s: s.file):
        hasher.update(section.file.encode("utf-8") + b"\0" + section.text.encode("utf-8") + b"\0")
    return hasher.hexdigest()[:DIGEST_CHARS]


def metadata(section: Section, corpus_digest: str) -> dict:
    """The document's metadata file, in the shape a Bedrock knowledge base reads from S3."""
    return {
        "metadataAttributes": {
            "runbook": section.runbook.file,
            "title": section.runbook.title,
            "section": section.name,
            "corpus": corpus_digest,
        }
    }


def is_ours(path: Path) -> bool:
    return path.is_file() and (path.name.endswith(METADATA_SUFFIX) or path.suffix == ".md")


def write(corpus: Corpus, out: Path = OUT) -> Path:
    """Write the documents and their metadata to `out`, replacing an earlier build there.

    Refuses a folder holding anything but an earlier build's files, so a wrong
    path can't delete something else. The manifest (the digest, the counts, each
    document's name) goes beside the folder, not in it, so it never becomes a
    document itself. Returns the manifest's path.
    """
    if out.exists():
        foreign = sorted(path.name for path in out.iterdir() if not is_ours(path))
        if foreign:
            raise FormatError(f"{out} holds files this build didn't write: {', '.join(foreign)}")
        for path in out.iterdir():
            path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    for section in corpus.sections:
        (out / section.file).write_bytes(section.text.encode("utf-8"))
        document = json.dumps(metadata(section, corpus.digest), indent=2) + "\n"
        (out / (section.file + METADATA_SUFFIX)).write_bytes(document.encode("utf-8"))
    manifest = out.with_suffix(".json")
    summary = {
        "corpus": corpus.digest,
        "runbooks": len(corpus.runbooks),
        "sections": len(corpus.sections),
        "documents": sorted(section.file for section in corpus.sections),
    }
    manifest.write_bytes((json.dumps(summary, indent=2) + "\n").encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--runbooks", type=Path, default=RUNBOOKS, help="the runbooks folder")
    parser.add_argument("--out", type=Path, default=OUT, help="where the documents go")
    args = parser.parse_args(argv)
    try:
        corpus = load(args.runbooks)
        manifest = write(corpus, args.out)
    except FormatError as exc:
        print(f"Runbooks not built:\n{exc}", file=sys.stderr)
        return 1
    print(
        f"{len(corpus.sections)} sections from {len(corpus.runbooks)} runbooks, corpus {corpus.digest}, "
        f"in {args.out} (manifest {manifest.name})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
