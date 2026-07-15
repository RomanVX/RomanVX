"""Перенос данных кабинета из Neon в Render Postgres (в свою схему).

Запуск (с любой машины, где есть python + psycopg2):
    python migrate_db.py --src "postgresql://...neon..." \
                         --dst "postgresql://...render-EXTERNAL..." \
                         --schema biomed

Копирует все таблицы public-схемы источника в указанную схему приёмника.
Устойчив к обрывам: при разрыве соединения переподключается и повторяет
текущую таблицу заново (до 5 попыток). Повторный запуск безопасен.
"""
import argparse
import time

import psycopg2
from psycopg2.extras import execute_batch

KEEPALIVE = dict(connect_timeout=15, keepalives=1, keepalives_idle=20,
                 keepalives_interval=10, keepalives_count=3)


def connect(url):
    return psycopg2.connect(url, **KEEPALIVE)


def copy_table(args, t):
    """Копирует одну таблицу целиком (свои соединения — обрыв не задевает остальных)."""
    src, dst = connect(args.src), connect(args.dst)
    sc, dc = src.cursor(), dst.cursor()

    sc.execute("""SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                  WHERE table_schema='public' AND table_name=%s
                  ORDER BY ordinal_position""", (t,))
    cols = sc.fetchall()
    col_defs = ", ".join(
        f'"{n}" {dt}' + ("" if nullable == "YES" else " NOT NULL")
        for n, dt, nullable in cols)
    col_names = ", ".join(f'"{n}"' for n, _, _ in cols)

    # первичный ключ источника
    sc.execute("""SELECT a.attname FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                  WHERE i.indrelid=('public.'||quote_ident(%s))::regclass AND i.indisprimary""", (t,))
    pk = [r[0] for r in sc.fetchall()]
    pk_sql = f', PRIMARY KEY ({", ".join(chr(34)+c+chr(34) for c in pk)})' if pk else ""

    full = f'"{args.schema}"."{t}"'
    dc.execute(f'DROP TABLE IF EXISTS {full}')
    dc.execute(f'CREATE TABLE {full} ({col_defs}{pk_sql})')
    dst.commit()

    sc.execute(f'SELECT {col_names} FROM public."{t}"')
    ph = ", ".join(["%s"] * len(cols))
    ins = f'INSERT INTO {full} ({col_names}) VALUES ({ph})'
    n = 0
    while True:
        rows = sc.fetchmany(1000)
        if not rows:
            break
        execute_batch(dc, ins, rows, page_size=200)
        dst.commit()
        n += len(rows)
        print(f"  {t}: {n} строк", end="\r")
    print(f"  {t}: {n} строк — готово        ")
    src.close()
    dst.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="URL источника (Neon)")
    ap.add_argument("--dst", required=True, help="URL приёмника (Render, External URL)")
    ap.add_argument("--schema", required=True, help="схема в приёмнике: biomed / fk")
    ap.add_argument("--only", default="", help="скопировать только эти таблицы (через запятую)")
    args = ap.parse_args()

    src, dst = connect(args.src), connect(args.dst)
    sc, dc = src.cursor(), dst.cursor()
    dc.execute(f'CREATE SCHEMA IF NOT EXISTS "{args.schema}"')
    dst.commit()
    sc.execute("""SELECT table_name FROM information_schema.tables
                  WHERE table_schema='public' AND table_type='BASE TABLE'
                  ORDER BY table_name""")
    tables = [r[0] for r in sc.fetchall()]
    src.close()
    dst.close()
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        tables = [t for t in tables if t in want]
    print(f"Таблиц к переносу: {len(tables)}: {', '.join(tables)}")

    for t in tables:
        for attempt in range(1, 6):
            try:
                copy_table(args, t)
                break
            except psycopg2.Error as e:
                msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
                print(f"  {t}: обрыв ({msg}) — попытка {attempt}/5, повтор через 5с")
                time.sleep(5)
        else:
            raise SystemExit(f"Таблица {t} не скопировалась за 5 попыток — запусти ещё раз "
                             f"с --only {t}")

    print(f"Миграция в схему {args.schema} завершена.")


if __name__ == "__main__":
    main()
