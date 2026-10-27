"""Release 1.43.0 is 1.42.0 without the null check, and its frames read as the service's own."""

import traceback
from pathlib import Path

import pytest

from checkout import VERSION, pricing
from checkout.catalog import PRODUCTS
from sim import releases

ITEMS = [(PRODUCTS["p-1003"], 1)]  # 3200 cents


@pytest.fixture
def bad_release():
    releases.deploy(releases.BAD_RELEASE)
    yield
    releases.deploy(VERSION)


def test_the_healthy_release_prices_a_cart_without_a_promo():
    assert pricing.price_cart(ITEMS, None) == 3200


def test_the_bad_release_fails_without_a_promo_inside_apply_promo(bad_release):
    with pytest.raises(TypeError, match="not subscriptable") as caught:
        pricing.price_cart(ITEMS, None)
    frame = traceback.extract_tb(caught.value.__traceback__)[-1]
    assert (Path(frame.filename), frame.name) == (Path(pricing.__file__), "apply_promo")
    # The line number is 1.43.0's own: the line that subscripts the missing promo.
    source = Path(pricing.__file__).read_text(encoding="utf-8").replace(releases.NULL_CHECK, "")
    assert 'promo["percent_off"]' in source.splitlines()[frame.lineno - 1]


def test_the_bad_release_still_prices_a_cart_with_a_promo(bad_release):
    assert pricing.price_cart(ITEMS, {"percent_off": 10}) == 2880


def test_names_bound_before_the_deploy_run_the_new_code(bad_release):
    from checkout.app import price_cart  # bound when app.py was imported

    with pytest.raises(TypeError):
        price_cart(ITEMS, None)


def test_deploying_the_healthy_release_rolls_back():
    releases.deploy(releases.BAD_RELEASE)
    releases.deploy(VERSION)
    assert pricing.price_cart(ITEMS, None) == 3200


def test_the_null_check_is_the_whole_difference():
    source = Path(pricing.__file__).read_text(encoding="utf-8")
    assert source.count(releases.NULL_CHECK) == 1


def test_an_unknown_release_is_refused():
    with pytest.raises(ValueError, match="no release 9.9.9"):
        releases.deploy("9.9.9")
