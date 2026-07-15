"""Снапшоты кешей в БД (kv_cache): переживают рестарт/сон Render.

Схема stale-while-revalidate: после холодного старта эндпоинт мгновенно
отдаёт последний сохранённый результат из БД, а свежие данные тянутся фоном.
Большие значения сжимаются zlib (сырые заказы WB за 90 дней — мегабайты).
"""
import base64
import hashlib
import json
import logging
import time as _time
import zlib

_log = logging.getLogger("snapshot")
_GZ_PREFIX = "gz:"
_GZ_MIN = 32 * 1024  # сжимать только крупные значения

# ЭКОНОМИЯ ТРАФИКА БД (у Neon free квота на data transfer):
# крупные ключи пишем не чаще раза в _BIG_INTERVAL и пропускаем запись,
# если содержимое не изменилось (сравниваем hash с прошлой записи).
_BIG_BYTES = 256 * 1024      # «крупным» считаем payload больше 256 КБ
_BIG_INTERVAL = 6 * 3600     # большие ключи — не чаще раза в 6 часов
_last_write: dict[str, float] = {}   # key → monotonic последней записи
_last_hash: dict[str, str] = {}      # key → md5 последней записи


def _should_write(key: str, payload: str) -> bool:
    h = hashlib.md5(payload.encode()).hexdigest()
    if _last_hash.get(key) == h:
        _log.info("snapshot %s: без изменений — запись пропущена", key)
        return False
    if len(payload) > _BIG_BYTES:
        last = _last_write.get(key, 0.0)
        if last and _time.monotonic() - last < _BIG_INTERVAL:
            _log.info("snapshot %s: крупный ключ, пишем не чаще раза в %dч — пропуск",
                      key, _BIG_INTERVAL // 3600)
            return False
    _last_hash[key] = h
    _last_write[key] = _time.monotonic()
    return True


def save(key: str, obj) -> None:
    """Сохраняет объект в kv_cache (перезаписывая прошлое значение)."""
    try:
        import db
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        if len(payload) > _GZ_MIN:
            payload = _GZ_PREFIX + base64.b64encode(
                zlib.compress(payload.encode(), 1)).decode()
        if not _should_write(key, payload):
            return
        db.execute("CREATE TABLE IF NOT EXISTS kv_cache (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("DELETE FROM kv_cache WHERE k = ?", (key,))
        db.execute("INSERT INTO kv_cache (k, v) VALUES (?, ?)", (key, payload))
    except Exception as exc:
        _log.warning("snapshot save %s failed: %s", key, exc)


def save_parts(key: str, meta: dict, parts: dict) -> None:
    """Как save(), но для больших значений: словарь meta + именованные списки
    parts сериализуются в zlib-компрессор кусками по 2000 строк, без
    гигантской JSON-строки в памяти (важно на 512 МБ Render).
    Формат на выходе тот же — читается обычным load()."""
    try:
        import db
        comp = zlib.compressobj(1)
        chunks: list[bytes] = []

        def feed(s: str) -> None:
            chunks.append(comp.compress(s.encode()))

        head = json.dumps(meta, ensure_ascii=False, default=str)
        feed(head[:-1])  # без закрывающей "}"
        first = head == "{}"
        for name, rows in parts.items():
            feed(('' if first else ',') + json.dumps(name) + ':[')
            first = False
            for i in range(0, len(rows), 2000):
                part = json.dumps(rows[i:i + 2000], ensure_ascii=False,
                                  default=str)[1:-1]  # содержимое без [ ]
                if part:
                    feed((',' if i else '') + part)
            feed(']')
        feed('}')
        chunks.append(comp.flush())
        payload = _GZ_PREFIX + base64.b64encode(b''.join(chunks)).decode()
        if not _should_write(key, payload):
            return
        db.execute("CREATE TABLE IF NOT EXISTS kv_cache (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("DELETE FROM kv_cache WHERE k = ?", (key,))
        db.execute("INSERT INTO kv_cache (k, v) VALUES (?, ?)", (key, payload))
        import heavy
        _log.info("snapshot %s: %s, %d КБ сжато (rss %.0f МБ)", key,
                  ", ".join(f"{n}={len(r)}" for n, r in parts.items()),
                  len(payload) // 1024, heavy.rss_mb())
    except Exception as exc:
        _log.warning("snapshot save_parts %s failed: %s", key, exc)


def save_rows(key: str, meta: dict, rows: list) -> None:
    """Частный случай save_parts: meta + один список под ключом rows."""
    save_parts(key, meta, {"rows": rows})


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
