"""code: which files may be read or patched, exactly as named, and a read's round trip."""

import hashlib
from pathlib import Path

import pytest

from sentinelops_core.code import MAX_PATH_CHARS, FileRead, digest, parse_file, readable, render_file

CHECKOUT = Path(__file__).parents[2] / "services" / "checkout-api" / "checkout"
# Read as text, so a CRLF checkout reads as LF, as the snapshot will hold it.
MODULES = {f"checkout/{p.name}": p.read_text(encoding="utf-8") for p in sorted(CHECKOUT.glob("*.py"))}
DOTLESS_I = chr(0x131)  # looks like i; ASCII only in .py files, so built here


def test_every_module_under_checkout_is_readable() -> None:
    assert "checkout/pricing.py" in MODULES and "checkout/__init__.py" in MODULES
    assert all(readable(path) for path in MODULES)


@pytest.mark.parametrize(
    "path",
    [
        # What knows the answer, or isn't the service's code.
        "handler.py",
        "sim/flags.py",
        "sim/releases.py",
        "controller/app.py",
        "traffic/generator.py",
        "tools/live_run.sh",
        "tests/test_app.py",
        "pyproject.toml",
        "../../runbooks/bad-deploy.md",
        # Right file, wrong spelling: refused, never repaired.
        "services/checkout-api/checkout/pricing.py",
        "/checkout/pricing.py",
        "./checkout/pricing.py",
        "checkout//pricing.py",
        "checkout/./pricing.py",
        "checkout/../handler.py",
        "checkout/../checkout/pricing.py",
        "checkout\\pricing.py",
        "Checkout/pricing.py",
        "checkout/Pricing.py",
        "checkout/pricing.PY",
        "checkout/pricing.pyc",
        "checkout/pricing",
        "checkout/",
        "checkout/sub/pricing.py",
        "checkout/pricing.py/",
        " checkout/pricing.py",
        "checkout/pricing.py ",
        "checkout/pricing.py\n",
        "checkout/pricing.py\x00.md",
        "checkout/%2e%2e/handler.py",
        "checkout/pricing%2epy",
        "checkout/*.py",
        "checkout/pr" + DOTLESS_I + "cing.py",
        "checkout/" + "a" * MAX_PATH_CHARS + ".py",
        "",
    ],
)
def test_anything_else_is_refused(path: str) -> None:
    assert not readable(path)


@pytest.mark.parametrize("path", [None, 1, b"checkout/pricing.py", ["checkout/pricing.py"], {"path": "checkout/pricing.py"}])
def test_a_path_that_isnt_a_string_is_refused(path: object) -> None:
    assert not readable(path)


@pytest.mark.parametrize("path", sorted(MODULES))
def test_a_read_of_each_module_parses_back_to_the_file(path: str) -> None:
    shown = render_file("1.42.0", path, MODULES[path])
    assert parse_file(shown) == FileRead("1.42.0", path, MODULES[path])


def test_a_read_numbers_every_line_as_cat_n_does_under_its_name_and_release() -> None:
    shown = render_file("1.43.0", "checkout/errors.py", "a = 1\n\n    b = 2\n")
    assert shown == "checkout/errors.py at 1.43.0: 3 lines\n     1\ta = 1\n     2\t\n     3\t    b = 2"


@pytest.mark.parametrize(("text", "head"), [("", "0 lines"), ("x = 1\n", "1 line"), ("\n", "1 line")])
def test_an_empty_or_one_line_file_round_trips(text: str, head: str) -> None:
    shown = render_file("1.42.0", "checkout/errors.py", text)
    assert shown.split("\n")[0] == f"checkout/errors.py at 1.42.0: {head}"
    assert parse_file(shown) == FileRead("1.42.0", "checkout/errors.py", text)


def test_a_line_holding_a_tab_or_digits_reads_back_as_it_was() -> None:
    text = "x = '\t'\n12\tafter\n"
    assert parse_file(render_file("1.42.0", "checkout/db.py", text)) == FileRead("1.42.0", "checkout/db.py", text)


@pytest.mark.parametrize(
    ("version", "path", "text"),
    [
        ("1.42.0", "handler.py", "x = 1\n"),
        ("1.42.0", "./checkout/db.py", "x = 1\n"),
        ("latest", "checkout/db.py", "x = 1\n"),
        ("1.42", "checkout/db.py", "x = 1\n"),
        ("1.42.0", "checkout/db.py", "x = 1\r\n"),
        ("1.42.0", "checkout/db.py", "x = 1"),
    ],
)
def test_render_refuses_what_it_cant_show_faithfully(version: str, path: str, text: str) -> None:
    with pytest.raises(ValueError):
        render_file(version, path, text)


PRICING = render_file("1.43.0", "checkout/pricing.py", MODULES["checkout/pricing.py"])
ROWS = PRICING.split("\n")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Could not read checkout/handler.py: only checkout/'s own modules can be read.",
        "\n".join(ROWS[:-1]),  # the last line missing
        "\n".join(ROWS[:4]),  # cut short
        "\n".join([*ROWS, f"{len(ROWS):>6}\textra"]),  # a line past the count
        "\n".join([ROWS[0], *ROWS[2:3], *ROWS[1:2], *ROWS[3:]]),  # out of order
        "\n".join([ROWS[0], *(row.replace("\t", " ", 1) for row in ROWS[1:])]),  # not numbered as cat -n
        PRICING.replace("checkout/pricing.py at", "handler.py at", 1),  # a file it couldn't have read
        PRICING.replace("at 1.43.0", "at latest", 1),  # no release
        PRICING.replace("at 1.43.0:", "at 1.43.0, lines 1-12:", 1),  # a range, not the file
        "[source c1]\n" + PRICING,  # as the model saw it: the run checks the tool's own text
        PRICING + "\n",  # a trailing line the count doesn't cover
    ],
)
def test_anything_but_one_whole_file_parses_to_none(text: str) -> None:
    assert parse_file(text) is None


def test_the_digest_is_the_sha256_of_the_files_utf8_bytes() -> None:
    text = MODULES["checkout/pricing.py"]
    assert digest(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest(text) != digest(text.replace("\n", "\r\n"))
