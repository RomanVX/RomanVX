"""Батарея корреляционных разрезов CH-211Å × BTC.

Соглашение о лаге: k > 0 означает «CH опережает BTC на k дней»,
т.е. сравниваются CH(t) и BTC(t+k). Сдвиги делаются строго внутри окна —
пары через разрыв данных не образуются.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import signal, stats

MIN_PAIRS = 6  # меньше — корреляцию не считаем вообще


# ------------------------------------------------------------ примитивы -----

def _corr(x: np.ndarray, y: np.ndarray) -> tuple[float, int]:
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < MIN_PAIRS or x.std() == 0 or y.std() == 0:
        return np.nan, len(x)
    return float(np.corrcoef(x, y)[0, 1]), len(x)


MODE_COLS = {
    "levels": ("ch_z", "btc_z"),
    "diffs": ("dch", "btc_ret"),
    "levels_dt": ("ch_dt", "btc_dt"),  # уровни после удаления медленного тренда
}


def lagged_pairs(pairs: pd.DataFrame, k: int, mode: str, window: str | None = None):
    """Массивы (ch_t, btc_{t+k}) c внутриоконным сдвигом.

    mode: 'levels' — z-уровни; 'diffs' — ΔCH и лог-доходность BTC;
          'levels_s3' — z-уровни, сглаженные MA(3);
          'levels_dt' — z-уровни минус скользящий тренд (колонки ch_dt/btc_dt
          должны быть добавлены в pairs заранее).
    """
    xs, ys = [], []
    for w, g in pairs.groupby("window"):
        if window is not None and w != window:
            continue
        if mode in MODE_COLS:
            cx, cy = MODE_COLS[mode]
            x, y = g[cx].to_numpy(), g[cy].to_numpy()
        elif mode == "levels_s3":
            x = g["ch_z"].rolling(3, center=True, min_periods=2).mean().to_numpy()
            y = g["btc_z"].rolling(3, center=True, min_periods=2).mean().to_numpy()
        else:
            raise ValueError(mode)
        if k >= 0:
            xs.append(x[: len(x) - k] if k else x)
            ys.append(y[k:])
        else:
            xs.append(x[-k:])
            ys.append(y[: len(y) + k])
    if not xs:
        return np.array([]), np.array([])
    return np.concatenate(xs), np.concatenate(ys)


# ------------------------------------------------------------- лаг-скан -----

@dataclass
class LagResult:
    k: int
    r: float
    n: int
    mode: str
    scope: str  # 'pooled' | имя окна


def lag_scan(pairs: pd.DataFrame, mode: str, scope: str = "pooled",
             kmax: int | None = None) -> list[LagResult]:
    if scope == "pooled":
        n_min = min(len(g) for _, g in pairs.groupby("window"))
    else:
        n_min = len(pairs[pairs["window"] == scope])
    kcap = max(2, n_min - MIN_PAIRS)
    if kmax is not None:
        kcap = min(kcap, kmax)
    out = []
    for k in range(-kcap, kcap + 1):
        x, y = lagged_pairs(pairs, k, mode, None if scope == "pooled" else scope)
        r, n = _corr(x, y)
        out.append(LagResult(k=k, r=r, n=n, mode=mode, scope=scope))
    return out


def best_lag(results: list[LagResult], causal_only: bool = False) -> LagResult:
    cand = [r for r in results if np.isfinite(r.r) and (r.k >= 0 or not causal_only)]
    return max(cand, key=lambda r: abs(r.r))


# ------------------------------------------------------- скользящая corr ----

def rolling_corr(pairs: pd.DataFrame, window_name: str, win: int = 10,
                 mode: str = "levels") -> pd.DataFrame:
    g = pairs[pairs["window"] == window_name].set_index("date")
    if mode == "levels":
        x, y = g["ch_z"], g["btc_z"]
    else:
        x, y = g["dch"], g["btc_ret"]
    r = x.rolling(win, min_periods=win).corr(y)
    return pd.DataFrame({"date": g.index, "r": r.to_numpy()}).dropna()


# ------------------------------------------------------------- события ------

def find_ch_events(ch: pd.DataFrame, prominence: float = 2.5) -> pd.DataFrame:
    """Локальные пики/впадины площади КД внутри каждого окна."""
    rows = []
    for w, g in ch.groupby("window"):
        y = g["ch211_pct"].to_numpy()
        for kind, arr in (("peak", y), ("trough", -y)):
            idx, _ = signal.find_peaks(arr, prominence=prominence)
            for i in idx:
                rows.append({
                    "date": g["date"].iloc[i], "kind": kind,
                    "ch211_pct": g["ch211_pct"].iloc[i], "window": w,
                })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def event_study(btc: pd.DataFrame, events: pd.DataFrame, pre: int = 4,
                post: int = 8) -> dict:
    """Путь BTC вокруг событий, нормированный к 100 в день события."""
    b = btc.set_index("date")["close_usd"]
    b = b.reindex(pd.date_range(b.index.min(), b.index.max(), freq="D")).interpolate(limit=2)
    out = {}
    for kind, g in events.groupby("kind"):
        mats = []
        for _, ev in g.iterrows():
            t0 = ev["date"]
            seg = []
            base = b.get(t0, np.nan)
            for d in range(-pre, post + 1):
                v = b.get(t0 + pd.Timedelta(days=d), np.nan)
                seg.append(100.0 * v / base if np.isfinite(base) else np.nan)
            mats.append((ev["date"], np.array(seg)))
        out[kind] = {"days": np.arange(-pre, post + 1), "paths": mats}
    return out
