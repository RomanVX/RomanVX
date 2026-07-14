"""Снапшоты кешей в БД (kv_cache): переживают рестарт/сон Render.

Схема stale-while-revalidate: после холодного старта эндпоинт мгновенно
отдаёт последний сохранённый результат из БД, а свежие данные тянутся фоном.
Большие значения сжимаются zlib (сырые заказы WB за 90 дней — мегабайты).
"""
import base64
import json
import logging
import zlib

_log = logging.getLogger("snapshot")
_GZ_PREFIX = "gz:"
_GZ_MIN = 32 * 1024  # сжимать только крупные значения


def save(key: str, obj) -> None:
    """Сохраняет объект в kv_cache (перезаписывая прошлое значение)."""
    try:
        import db
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        if len(payload) > _GZ_MIN:
            payload = _GZ_PREFIX + base64.b64encode(
                zlib.compress(payload.encode(), 1)).decode()
        db.execute("CREATE TABLE IF NOT EXISTS kv_cache (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("DELETE FROM kv_cache WHERE k = ?", (key,))
        db.execute("INSERT INTO kv_cache (k, v) VALUES (?, ?)", (key, payload))
    except Exception as exc:
        _log.warning("snapshot save %s failed: %s", key, exc)


def save_rows(key: str, meta: dict, rows: list) -> None:
    """Как save(), но для больших списков строк: JSON пишется в zlib-компрессор
    кусками, без гигантской строки в памяти (важно на 512 МБ Render).
    Формат на выходе тот же — читается обычным load()."""
    try:
        import db
        comp = zlib.compressobj(1)
        chunks: list[bytes] = []
        head = json.dumps({**meta, "rows": []}, ensure_ascii=False, default=str)
        chunks.append(comp.compress(head[:-2].encode()))  # без закрывающих "]}"
        for i in range(0, len(rows), 2000):
            part = json.dumps(rows[i:i + 2000], ensure_ascii=False,
                              default=str)[1:-1]  # содержимое без [ ]
            if part:
                chunks.append(comp.compress(
                    ((',' if i else '') + part).encode()))
        chunks.append(comp.compress(b']}'))
        chunks.append(comp.flush())
        payload = _GZ_PREFIX + base64.b64encode(b''.join(chunks)).decode()
        db.execute("CREATE TABLE IF NOT EXISTS kv_cache (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("DELETE FROM kv_cache WHERE k = ?", (key,))
        db.execute("INSERT INTO kv_cache (k, v) VALUES (?, ?)", (key, payload))
        _log.info("snapshot %s: %d rows, %d KB сжато", key, len(rows), len(payload) // 1024)
    except Exception as exc:
        _log.warning("snapshot save_rows %s failed: %s", key, exc)


def load(key: str, default=None):
    """Читает объект из kv_cache; при любой ошибке возвращает default."""
    try:
        import db
        rows = db.fetchall("SELECT v FROM kv_cache WHERE k = ?", (key,))
        if not rows:
            return default
        payload = rows[0][0]
        if payload.startswith(_GZ_PREFIX):
            payload = zlib.decompress(
                base64.b64decode(payload[len(_GZ_PREFIX):])).decode()
        return json.loads(payload)
    except Exception as exc:
        _log.warning("snapshot load %s failed: %s", key, exc)
        return default
