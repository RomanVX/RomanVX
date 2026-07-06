"""Загрузка данных. Приоритет источников CH-211:

1. data/sinp_qlookdata.txt — почасовая выгрузка SINP (реальные данные);
2. data/sinp_export.csv   — CSV-экспорт графика (меню ≡ → Download CSV);
3. data/ch211_daily.csv   — оцифровка скриншотов (fallback).

BTC: data/btc_export.csv (точная выгрузка) > data/btc_usd_daily.csv
(оцифровка свечей + датированные якоря из поиска).
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QLOOK = DATA_DIR / "sinp_qlookdata.txt"


# ------------------------------------------------------------ helpers -------

def _label_contiguous_windows(dates: pd.Series, max_gap_days: int = 3) -> pd.Series:
    """Метки непрерывных отрезков: разрыв > max_gap_days начинает новое окно."""
    gap = dates.diff().dt.days.fillna(1)
    ids = (gap > max_gap_days).cumsum()
    return ids.map(lambda i: f"w{i + 1}")


def _daily_from_hourly(dt: pd.Series, val: pd.Series,
                       interp_limit: int = 2) -> pd.DataFrame:
    """Дневные средние из почасовых значений; дырки ≤ interp_limit дней
    интерполируются, длинные разрывы остаются NaN и режут окна."""
    d = (
        pd.DataFrame({"date": dt.dt.normalize(), "v": val})
        .groupby("date", as_index=False)["v"].mean()
    )
    full = pd.DataFrame({"date": pd.date_range(d["date"].min(), d["date"].max())})
    d = full.merge(d, on="date", how="left")
    d["v"] = d["v"].interpolate(limit=interp_limit, limit_area="inside")
    return d.dropna(subset=["v"]).reset_index(drop=True)


# ----------------------------------------------------------- SINP qlook -----

def _parse_qlook_hourly() -> pd.DataFrame:
    """Формат qlookdata: шапка-комментарий, затем
    'YYYY-MM-DD HH:MM:SS.mmm , v1 , v2 , v3 , v4 , v5' с 'N/A' в пропусках."""
    lines = QLOOK.read_text(encoding="utf-8-sig").splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^\d{4}-\d{2}-\d{2}", l))
    rows = []
    for l in lines[start:]:
        parts = [p.strip() for p in l.split(",")]
        dt = pd.to_datetime(parts[0], errors="coerce")
        if pd.isna(dt):
            continue
        vals = []
        for p in parts[1:6]:
            try:
                vals.append(float(p))
            except ValueError:
                vals.append(np.nan)
        vals += [np.nan] * (5 - len(vals))
        rows.append([dt, *vals])
    return pd.DataFrame(rows, columns=["dt", "ch193", "ch211", "sw193", "sw211", "bulk"])


# ---------------------------------------------------------------- CH 211A ---

def load_ch211() -> tuple[pd.DataFrame, str]:
    """-> (df[date, ch211_pct, sigma_pct, window], описание источника)."""
    if QLOOK.exists():
        h = _parse_qlook_hourly()
        d = _daily_from_hourly(h["dt"], h["ch211"])
        d = d.rename(columns={"v": "ch211_pct"})
        d["sigma_pct"] = 0.0
        d["window"] = _label_contiguous_windows(d["date"])
        return d, "sinp_qlookdata.txt (реальная почасовая выгрузка SINP → дневные средние)"

    export = DATA_DIR / "sinp_export.csv"
    if export.exists():
        d = _parse_sinp_export(export)
        return d, "sinp_export.csv (точная выгрузка)"

    d = pd.read_csv(DATA_DIR / "ch211_daily.csv", parse_dates=["date"])
    d = d[["date", "ch211_pct", "sigma_pct", "window"]].sort_values("date")
    return d.reset_index(drop=True), "ch211_daily.csv (оцифровка скриншотов)"


def _parse_sinp_export(path: Path) -> pd.DataFrame:
    """Экспорт Highcharts: 1-я колонка DateTime, далее по колонке на серию."""
    raw = path.read_text(encoding="utf-8-sig")
    header = raw.splitlines()[0]
    sep = ";" if header.count(";") > header.count(",") else ","
    df = pd.read_csv(io.StringIO(raw), sep=sep)
    ch_cols = [c for c in df.columns if "211" in str(c)]
    if not ch_cols:
        raise ValueError(f"В {path.name} нет колонки с '211'; колонки: {list(df.columns)}")
    col = ch_cols[0]
    if df[col].dtype == object:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace('"', "").str.replace(",", ".", regex=False),
            errors="coerce",
        )
    dt = pd.to_datetime(df.iloc[:, 0], errors="coerce")
    d = _daily_from_hourly(dt, df[col]).rename(columns={"v": "ch211_pct"})
    d["sigma_pct"] = 0.0
    d["window"] = _label_contiguous_windows(d["date"])
    return d


# ----------------------------------------------------- солнечный ветер ------

def load_solar_wind() -> pd.DataFrame | None:
    """Дневная фактическая скорость СВ (Bulk speed) из qlookdata, если есть."""
    if not QLOOK.exists():
        return None
    h = _parse_qlook_hourly()
    d = _daily_from_hourly(h["dt"], h["bulk"]).rename(columns={"v": "bulk_kms"})
    return d


# -------------------------------------------------------------------- BTC ---

_DATE_RX = re.compile(r"(date|time|дата|open_time)", re.I)
_CLOSE_RX = re.compile(r"(close|price|цена|закрыт)", re.I)


def load_btc() -> tuple[pd.DataFrame, str]:
    """-> (df[date, close_usd, sigma_usd], источник). Неполные дни отброшены."""
    export = DATA_DIR / "btc_export.csv"
    if export.exists():
        return _parse_btc_export(export), "btc_export.csv (точная выгрузка)"
    df = pd.read_csv(DATA_DIR / "btc_usd_daily.csv", parse_dates=["date"])
    df = df[~df["provenance"].str.contains("partial", na=False)]
    df = df[["date", "close_usd", "sigma_usd"]].sort_values("date")
    return df.reset_index(drop=True), "btc_usd_daily.csv (оцифровка свечей + якоря из поиска)"


def _parse_btc_export(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    date_col = next((c for c in df.columns if _DATE_RX.search(str(c))), df.columns[0])
    close_col = next((c for c in df.columns if _CLOSE_RX.search(str(c))), None)
    if close_col is None:
        raise ValueError(f"В {path.name} нет колонки закрытия; колонки: {list(df.columns)}")
    close = df[close_col]
    if close.dtype == object:  # investing.com: "62,883.8"
        close = pd.to_numeric(
            close.astype(str).str.replace('"', "").str.replace(",", "", regex=False),
            errors="coerce",
        )
    dt = df[date_col]
    if np.issubdtype(dt.dtype, np.number):  # unix ms/s (Binance open_time)
        dt = pd.to_datetime(dt, unit="ms" if float(dt.iloc[0]) > 1e11 else "s")
    else:
        dt = pd.to_datetime(dt, errors="coerce")
    out = (
        pd.DataFrame({"date": dt.dt.normalize(), "close_usd": close})
        .dropna()
        .groupby("date", as_index=False)["close_usd"].last()
        .sort_values("date")
        .reset_index(drop=True)
    )
    out["sigma_usd"] = 0.0
    return out


# ------------------------------------------------------------------ пары ----

def make_pairs(ch: pd.DataFrame, other: pd.DataFrame, value_col: str = "close_usd",
               min_window: int = 10) -> pd.DataFrame:
    """Дневные пары CH × другой ряд.

    После склейки окна пере-размечаются по фактической непрерывности дат
    (сдвиги лагов требуют последовательных дней), z-нормировка уровней — внутри
    окна (иначе пул меряет разницу эпох, а не совместное движение). Окна короче
    min_window дней отбрасываются.
    """
    pairs = (
        ch[["date", "ch211_pct"]]
        .merge(other[["date", value_col]], on="date", how="inner")
        .sort_values("date")
        .reset_index(drop=True)
    )
    pairs["window"] = _label_contiguous_windows(pairs["date"], max_gap_days=1)
    sizes = pairs.groupby("window")["date"].transform("size")
    pairs = pairs[sizes >= min_window].reset_index(drop=True)
    # стабильные имена w1..wN после фильтрации
    remap = {w: f"w{i + 1}" for i, w in enumerate(pairs["window"].unique())}
    pairs["window"] = pairs["window"].map(remap)

    g = pairs.groupby("window")
    pairs["ch_z"] = g["ch211_pct"].transform(lambda s: (s - s.mean()) / s.std(ddof=1))
    pairs["btc_z"] = g[value_col].transform(lambda s: (s - s.mean()) / s.std(ddof=1))
    pairs["dch"] = g["ch211_pct"].diff()
    pairs["btc_ret"] = g[value_col].transform(lambda s: np.log(s).diff())
    if value_col != "close_usd":
        pairs = pairs.rename(columns={value_col: "value"})
    return pairs
