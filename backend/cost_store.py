"""In-memory store for supplier article → unit cost (₽).

Populated via POST /api/upload/costs; used by analytics.unit_economics().
"""
_costs: dict[str, float] = {}


def set_costs(mapping: dict[str, float]) -> None:
    _costs.clear()
    _costs.update(mapping)


def get_costs() -> dict[str, float]:
    return dict(_costs)


def count() -> int:
    return len(_costs)
