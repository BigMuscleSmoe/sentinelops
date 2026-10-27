"""Releases of checkout-api's code, for the bad deploy.

checkout/ is release 1.42.0. Release 1.43.0 is the same code with the null
check in pricing.apply_promo removed, so a checkout without a promo code
fails with a TypeError. It is built from checkout/pricing.py's own source and
compiled under that file's path, so its frames in a stack trace read
checkout/pricing.py like the real ones, and never sim/.

deploy() swaps each pricing function's code object, not the module attribute,
so names bound at import, like app.py's price_cart, run the new code too.
"""

import types
from pathlib import Path

from checkout import VERSION, pricing

BAD_RELEASE = "1.43.0"
RELEASES = (VERSION, BAD_RELEASE)
NULL_CHECK = "    if promo is None:\n        return subtotal_cents\n"

_PATH = Path(pricing.__file__)
_FUNCTIONS = {
    name: value
    for name, value in vars(pricing).items()
    if isinstance(value, types.FunctionType) and value.__module__ == pricing.__name__
}
_CODE = {VERSION: {name: function.__code__ for name, function in _FUNCTIONS.items()}}
_deployed = VERSION


def deploy(version: str) -> None:
    """Run `version` of the pricing code in this process from now on."""
    global _deployed
    if version == _deployed:
        return
    if version not in RELEASES:
        raise ValueError(f"no release {version}; there are {', '.join(RELEASES)}")
    if version not in _CODE:
        _CODE[version] = _build_bad_release()
    for name, code in _CODE[version].items():
        _FUNCTIONS[name].__code__ = code
    _deployed = version


def _build_bad_release() -> dict[str, types.CodeType]:
    source = _PATH.read_text(encoding="utf-8")
    if NULL_CHECK not in source:
        raise RuntimeError(f"{_PATH.name} no longer has the null check that {BAD_RELEASE} removes")
    namespace: dict[str, object] = {"__name__": pricing.__name__}
    exec(compile(source.replace(NULL_CHECK, "", 1), str(_PATH), "exec"), namespace)
    return {name: namespace[name].__code__ for name in _FUNCTIONS}  # type: ignore[attr-defined]
