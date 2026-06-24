"""DB layer — PostgreSQL (via DATABASE_URL) with SQLite fallback for local dev.

Postgres-соединения берутся из пула (ThreadedConnectionPool), а не открываются
заново на каждый запрос: новый TCP+TLS-хендшейк до Neon (Франкфурт) стоит сотни
мс, а на free-тарифе ещё и будит «уснувшую» БД. Пул держит соединения тёплыми.
"""
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

_log = logging.getLogger(__name__)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_PG = DATABASE_URL.startswith("postgres")

if IS_PG:
    SQLITE_PATH = None
    _log.info("DB: using PostgreSQL (pooled)")
else:
    SQLITE_PATH = os.environ.get("SQLITE_PATH", "/tmp/reviews.db")
    _log.info("DB: using SQLite at %s", SQLITE_PATH)


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    """Ленивая инициализация пула — отсутствие psycopg2 не валит модуль при импорте."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg2 import pool
                _pool = pool.ThreadedConnectionPool(
                    1, 8, DATABASE_URL, connect_timeout=10,
                    keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=3,
                )
    return _pool


def get_conn():
    """Совместимость: разовое соединение (для прямого доступа). Лучше — _conn()."""
    if IS_PG:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)
    return sqlite3.connect(SQLITE_PATH)


@contextmanager
def _conn():
    """Соединение из пула (PG) или разовое (SQLite). Возвращает в пул, не закрывает."""
    if not IS_PG:
        con = sqlite3.connect(SQLITE_PATH)
        try:
            yield con
        finally:
            con.close()
        return

    p = _get_pool()
    con = p.getconn()
    broken = False
    try:
        yield con
    except Exception:
        broken = True  # соединение могло остаться в битом состоянии — выкинуть из пула
        raise
    finally:
        if broken:
            try:
                con.close()
            except Exception:
                pass
            p.putconn(con, close=True)
        else:
            try:
                con.rollback()  # сброс любой незакрытой транзакции перед возвратом
            except Exception:
                pass
            p.putconn(con)


def _tr(sql: str) -> str:
    """Translate ? placeholders into %s for psycopg2."""
    return sql.replace("?", "%s") if IS_PG else sql


def execute(sql: str, params=()):
    with _conn() as con:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        con.commit()


def executemany(sql: str, seq):
    with _conn() as con:
        cur = con.cursor()
        cur.executemany(_tr(sql), list(seq))
        con.commit()


def fetchall(sql: str, params=()) -> list:
    with _conn() as con:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        return cur.fetchall()


def fetchone(sql: str, params=()):
    with _conn() as con:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        return cur.fetchone()
