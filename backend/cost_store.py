"""Persistent COGS store — PostgreSQL/SQLite backed, in-memory cache on top."""
import csv
import hashlib
import logging
import re
from pathlib import Path

import db

_log = logging.getLogger(__name__)

from config import CABINET as _CABINET
_SEED_PATH = Path(__file__).parent / "data" / (
    "cogs_seed_fk.csv" if _CABINET == "fk" else "cogs_seed.csv")

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


def _parse_seed() -> tuple[dict, dict, dict]:
    """cogs_seed.csv: SKU;nmId;категория;полное имя;короткое имя;себес ₽"""
    costs, names, nmids = {}, {}, {}
    with open(_SEED_PATH, encoding="utf-8-sig") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) < 6:
                continue
            sku = (row[0] or "").strip()
            raw_cost = re.sub(r"[₽\s\xa0]", "", str(row[5] or "")).replace(",", ".")
            if not sku or not raw_cost:
                continue
            try:
                costs[sku] = float(raw_cost)
            except ValueError:
                continue
            names[sku] = (row[4] or "").strip() or (row[3] or "").strip()
            try:
                nmids[sku] = int((row[1] or "").strip())
            except ValueError:
                pass
    return costs, names, nmids


def _apply_seed_if_new():
    """Загружает cogs_seed.csv в БД, если файл изменился с прошлого применения.

    Хеш файла хранится в cogs_meta — ручная загрузка через /api/upload/costs
    продолжает работать и не перезатирается до следующего изменения seed-файла.
    """
    if not _SEED_PATH.exists():
        return
    db.execute("CREATE TABLE IF NOT EXISTS cogs_meta (key TEXT PRIMARY KEY, value TEXT)")
    file_hash = hashlib.md5(_SEED_PATH.read_bytes()).hexdigest()
    row = db.fetchone("SELECT value FROM cogs_meta WHERE key = 'seed_hash'")
    if row and row[0] == file_hash:
        return
    costs, names, nmids = _parse_seed()
    if not costs:
        return
    set_costs(costs, names, nmids)
    if db.IS_PG:
        db.execute("INSERT INTO cogs_meta (key, value) VALUES ('seed_hash', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (file_hash,))
    else:
        db.execute("INSERT OR REPLACE INTO cogs_meta VALUES ('seed_hash', ?)", (file_hash,))
    _log.info("COGS seed applied: %d SKUs (hash %s)", len(costs), file_hash[:8])


def init():
    """Call once at startup."""
    _load_from_db()
    try:
        _apply_seed_if_new()
    except Exception as e:
        _log.warning("COGS seed failed: %s", e)


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
    if not _costs:
        from config import USE_MOCK
        if USE_MOCK:
            import mock_data
            return dict(mock_data.COSTS)
    return dict(_costs)


def get_names() -> dict[str, str]:
    if not _names:
        from config import USE_MOCK
        if USE_MOCK:
            import mock_data
            return dict(mock_data.NAMES)
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
