"""Перенос данных кабинета из Neon в Render Postgres (в свою схему).

Запуск (с любой машины, где есть python + psycopg2):
    python migrate_db.py --src "postgresql://...neon..." \
                         --dst "postgresql://...render-EXTERNAL..." \
                         --schema biomed

Копирует все таблицы public-схемы источника в указанную схему приёмника
(создаёт таблицы по фактической структуре, переносит строки пачками).
Повторный запуск безопасен: таблицы пересоздаются заново.
"""
import argparse

import psycopg2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="URL источника (Neon)")
    ap.add_argument("--dst", required=True, help="URL приёмника (Render, External URL)")
    ap.add_argument("--schema", required=True, help="схема в приёмнике: biomed / fk")
    args = ap.parse_args()

    src = psycopg2.connect(args.src)
    dst = psycopg2.connect(args.dst)
    sc, dc = src.cursor(), dst.cursor()

    dc.execute(f'CREATE SCHEMA IF NOT EXISTS "{args.schema}"')
    dst.commit()

    sc.execute("""SELECT table_name FROM information_schema.tables
                  WHERE table_schema='public' AND table_type='BASE TABLE'
                  ORDER BY table_name""")
    tables = [r[0] for r in sc.fetchall()]
    print(f"Таблиц в источнике: {len(tables)}: {', '.join(tables)}")

    for t in tables:
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
        n = 0
        while True:
            rows = sc.fetchmany(2000)
            if not rows:
                break
            dc.executemany(f'INSERT INTO {full} ({col_names}) VALUES ({ph})', rows)
            dst.commit()
            n += len(rows)
            print(f"  {t}: {n} строк", end="\r")
        print(f"  {t}: {n} строк — готово        ")

    src.close()
    dst.close()
    print(f"Миграция в схему {args.schema} завершена.")


if __name__ == "__main__":
    main()
