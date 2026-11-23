"""patches: a fix as exact-text edits, every check it must pass, and the diff code renders from it."""

import pytest

from sentinelops_core.patches import MAX_CHANGED_LINES, MAX_EDITS, MAX_NEW_CHARS, Refused, apply_edits

PATH = "checkout/pricing.py"
ORIGINAL = '''"""Order pricing."""

from collections.abc import Mapping
from typing import Any


def apply_promo(subtotal_cents: int, promo: Mapping[str, Any] | None) -> int:
    return subtotal_cents - subtotal_cents * promo["percent_off"] // 100


def round_cents(cents: int) -> int:
    return cents
'''
SIGNATURE = "def apply_promo(subtotal_cents: int, promo: Mapping[str, Any] | None) -> int:\n"
NULL_CHECK = SIGNATURE + "    if promo is None:\n        return subtotal_cents\n"
FIX = (SIGNATURE, NULL_CHECK)
SECOND = ("def round_cents(cents: int) -> int:\n    return cents\n", "def round_cents(cents: int) -> int:\n    return round(cents)\n")


def test_a_patch_that_passes_gives_the_file_and_a_diff_from_the_repos_root() -> None:
    checked = apply_edits(PATH, ORIGINAL, [FIX])

    assert checked.text == ORIGINAL.replace(SIGNATURE, NULL_CHECK)
    assert checked.diff.startswith(
        "--- a/services/checkout-api/checkout/pricing.py\n+++ b/services/checkout-api/checkout/pricing.py\n@@ "
    )
    added = [line for line in checked.diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert added == ["+    if promo is None:", "+        return subtotal_cents"]


def test_edits_apply_wherever_they_sit_in_whatever_order_they_come() -> None:
    forward = apply_edits(PATH, ORIGINAL, [FIX, SECOND])
    backward = apply_edits(PATH, ORIGINAL, [SECOND, FIX])
    assert forward == backward
    assert forward.text == ORIGINAL.replace(SIGNATURE, NULL_CHECK).replace(*SECOND)


def test_an_edit_may_delete_and_new_text_may_move_an_existing_import() -> None:
    moved = ("from collections.abc import Mapping\nfrom typing import Any\n", "from typing import Any\nfrom collections.abc import Mapping\n")
    assert apply_edits(PATH, ORIGINAL, [moved, ("\n\ndef round_cents(cents: int) -> int:\n    return cents\n", "")])


def test_nothing_in_a_patch_runs() -> None:
    # Checked as text and syntax only: the module-level exit is compiled, never executed.
    checked = apply_edits(PATH, ORIGINAL, [(SECOND[0], SECOND[0] + "\nraise SystemExit(3)\n")])
    assert "raise SystemExit(3)" in checked.text


@pytest.mark.parametrize(
    ("path", "edits", "reason"),
    [
        ("handler.py", [FIX], "a file that can't be patched"),
        ("./checkout/pricing.py", [FIX], "a file that can't be patched"),
        ("sim/releases.py", [FIX], "a file that can't be patched"),
        (PATH, [], "no edits"),
        (PATH, [FIX, SECOND, ("Any\n\n\n", "Any\n\n\n\n"), ('"""Order', '"""All order')], f"4 edits, past {MAX_EDITS}"),
        (PATH, [("", "x = 1\n")], "edit 1: old is empty"),
        (PATH, [FIX, (SIGNATURE, SIGNATURE)], "edit 2: changes nothing"),
        (PATH, [(SIGNATURE, NULL_CHECK.replace("\n", "\r\n"))], "edit 1: holds a carriage return"),
        (PATH, [(SIGNATURE, NULL_CHECK + "    # caf" + chr(0xE9) + "\n")], "edit 1: new text isn't ASCII"),
        (PATH, [(SIGNATURE.replace("int", "float", 1), NULL_CHECK)], "edit 1: old text isn't in the file"),
        (PATH, [("    return ", "    return 0 + ")], "edit 1: old text is in the file more than once"),
        (PATH, [FIX, ("Mapping[str, Any] | None) -> int:\n    return", "Mapping[str, Any]) -> int:\n    return")], "edits overlap"),
        (PATH, [(SECOND[0], SECOND[0] + "# " + "x" * MAX_NEW_CHARS + "\n")], f"more than {MAX_NEW_CHARS} characters of new text"),
        (PATH, [("    return cents\n", "    return cents")], "drops the file's last newline"),
        (PATH, [(SIGNATURE, "def apply_promo(:\n")], "doesn't parse"),
        (PATH, [("def round_cents(cents: int) -> int:\n    return cents\n", "return 1\n")], "doesn't compile"),
        (PATH, [(SIGNATURE, NULL_CHECK + "    import os\n")], "adds an import"),
        (PATH, [(SIGNATURE, "from os import path\n\n\n" + SIGNATURE)], "adds an import"),
        (PATH, [(SIGNATURE, "from typing import Any\n\n\n" + SIGNATURE)], "adds an import"),  # a second copy
        (PATH, [(SIGNATURE, "import collections.abc as Mapping\n\n\n" + SIGNATURE)], "adds an import"),
    ],
)
def test_a_patch_that_fails_a_check_is_refused_saying_which(path: str, edits: list[tuple[str, str]], reason: str) -> None:
    with pytest.raises(Refused) as refused:
        apply_edits(path, ORIGINAL, edits)
    assert str(refused.value) == reason


def test_a_patch_past_the_changed_line_limit_is_refused() -> None:
    padding = "".join(f"# note {n}\n" for n in range(MAX_CHANGED_LINES))
    with pytest.raises(Refused, match=f"^{MAX_CHANGED_LINES + 2} changed lines, past {MAX_CHANGED_LINES}$"):
        apply_edits(PATH, ORIGINAL, [(SIGNATURE, padding + NULL_CHECK)])
    assert apply_edits(PATH, ORIGINAL, [(SIGNATURE, padding[: padding.index("# note 38")] + NULL_CHECK)])


def test_a_file_that_didnt_parse_as_read_is_refused_even_if_the_patch_would_fix_it() -> None:
    broken = ORIGINAL.replace("    return cents\n", "    return (cents\n")
    with pytest.raises(Refused, match="^the file as read doesn't parse$"):
        apply_edits(PATH, broken, [("    return (cents\n", "    return cents\n")])
