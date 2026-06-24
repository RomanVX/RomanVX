"""DB layer — PostgreSQL (via DATABASE_URL) with SQLite fallback for local dev."""
import logging
import os
import sqlite3

_log = logging.getLogger(__name__)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_PG = DATABASE_URL.startswith("postgres")

if IS_PG:
    SQLITE_PATH = None
    _log.info("DB: using PostgreSQL")
else:
    SQLITE_PATH = os.environ.get("SQLITE_PATH", "/tmp/reviews.db")
    _log.info("DB: using SQLite at %s", SQLITE_PATH)


def get_conn():
    if IS_PG:
        import psycopg2  # ленивый импорт — отсутствие пакета не валит весь модуль
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)
    return sqlite3.connect(SQLITE_PATH)


def _tr(sql: str) -> str:
    """Translate ? placeholders into %s for psycopg2."""
    return sql.replace("?", "%s") if IS_PG else sql


def execute(sql: str, params=()):
    con = get_conn()
    try:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        con.commit()
    finally:
        con.close()


def executemany(sql: str, seq):
    con = get_conn()
    try:
        cur = con.cursor()
        cur.executemany(_tr(sql), list(seq))
        con.commit()
    finally:
        con.close()


def fetchall(sql: str, params=()) -> list:
    con = get_conn()
    try:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        return cur.fetchall()
    finally:
        con.close()


def fetchone(sql: str, params=()):
    con = get_conn()
    try:
        cur = con.cursor()
        cur.execute(_tr(sql), params)
        return cur.fetchone()
    finally:
        con.close()
