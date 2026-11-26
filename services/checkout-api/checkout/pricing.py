"""Order pricing."""

from collections.abc import Mapping
from typing import Any

from checkout.catalog import Product


def price_cart(items: list[tuple[Product, int]], promo: Mapping[str, Any] | None) -> int:
    subtotal = sum(product.price_cents * quantity for product, quantity in items)
    return apply_promo(subtotal, promo)


def apply_promo(subtotal_cents: int, promo: Mapping[str, Any] | None) -> int:
    if promo is None:
        return subtotal_cents
    return subtotal_cents - subtotal_cents * promo["percent_off"] // 100
