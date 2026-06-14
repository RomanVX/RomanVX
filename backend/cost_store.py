"""In-memory store for supplier article → unit cost (₽) and product name."""
_costs: dict[str, float] = {}
_names: dict[str, str]   = {}


def set_costs(mapping: dict[str, float], names: dict[str, str] | None = None) -> None:
    _costs.clear()
    _costs.update(mapping)
    if names:
        _names.clear()
        _names.update(names)


def get_costs() -> dict[str, float]:
    return dict(_costs)


def get_names() -> dict[str, str]:
    return dict(_names)


def count() -> int:
    return len(_costs)
