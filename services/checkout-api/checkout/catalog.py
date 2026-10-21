"""The product catalog. Small and static: the demo needs realistic shapes, not data."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    price_cents: int


PRODUCTS = {
    p.id: p
    for p in (
        Product("p-1001", "Trail running shoes", 12900),
        Product("p-1002", "Merino base layer", 7400),
        Product("p-1003", "Insulated water bottle", 3200),
        Product("p-1004", "Headlamp, 400 lumen", 4500),
        Product("p-1005", "Rain shell", 18900),
        Product("p-1006", "Wool hiking socks", 2200),
        Product("p-1007", "Daypack, 22 L", 9800),
        Product("p-1008", "Trekking poles", 11500),
        Product("p-1009", "Camp stove", 6900),
        Product("p-1010", "First aid kit", 3900),
        Product("p-1011", "Sun hoodie", 5600),
        Product("p-1012", "Gaiters", 4100),
    )
}
