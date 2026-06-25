"""Persistent COGS store — PostgreSQL/SQLite backed, in-memory cache on top."""
import logging
import db

_log = logging.getLogger(__name__)

_costs: dict[str, float] = {}
_names: dict[str, str]   = {}
_nmids: dict[str, int]   = {}   # sku → nmId (WB article number)


def _init_table():
    db.execute("""
        CREATE TABLE IF NOT EXISTS cogs (
            sku      TEXT PRIMARY KEY,
            nm_id    BIGINT,
            name     TEXT,
            cost_rub REAL NOT NULL
        )
    """)


def _load_from_db():
    global _costs, _names, _nmids
    try:
        _init_table()
        rows = db.fetchall("SELECT sku, nm_id, name, cost_rub FROM cogs")
        _costs = {r[0]: r[3] for r in rows}
        _nmids = {r[0]: r[1] for r in rows if r[1]}
        _names = {r[0]: r[2] for r in rows if r[2]}
        _log.info("COGS loaded from DB: %d SKUs", len(_costs))
    except Exception as e:
        _log.warning("COGS DB load failed: %s", e)


def init():
    """Call once at startup."""
    _load_from_db()


def set_costs(
    mapping: dict[str, float],
    names: dict[str, str] | None = None,
    nmids: dict[str, int] | None = None,
) -> None:
    _init_table()
    rows = []
    for sku, cost in mapping.items():
        nm = (nmids or {}).get(sku)
        name = (names or {}).get(sku, "")
        rows.append((sku, nm, name, cost))

    db.execute("DELETE FROM cogs")
    db.executemany(
        "INSERT INTO cogs (sku, nm_id, name, cost_rub) VALUES (?, ?, ?, ?)",
        rows,
    )
    _load_from_db()
    _log.info("COGS saved: %d SKUs", len(mapping))


def get_costs() -> dict[str, float]:
    return dict(_costs)


def get_names() -> dict[str, str]:
    return dict(_names)


def get_nmids() -> dict[str, int]:
    return dict(_nmids)


def get_all() -> list[dict]:
    """Full COGS table for API response."""
    return [
        {"sku": sku, "name": _names.get(sku, ""), "nmId": _nmids.get(sku), "cost": cost}
        for sku, cost in sorted(_costs.items())
    ]


def count() -> int:
    return len(_costs)
