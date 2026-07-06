"""Честная статистика для коротких автокоррелированных рядов.

Три инструмента против самообмана:
1. Эффективный размер выборки (приближение Бартлетта): у гладких рядов
   независимых наблюдений в разы меньше, чем точек.
2. Циркулярные суррогаты: сохраняем автокорреляцию каждого ряда, разрушаем
   совместное выравнивание — получаем распределение max|r| по всему лаг-скану,
   т.е. p-value с учётом того, что лаг мы ПОДБИРАЛИ (multiple comparisons).
3. Блочный бутстрэп для доверительного интервала r на выбранном лаге.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .slices import MIN_PAIRS


# ------------------------------------------------------- эффективный N ------

def _acf(x: np.ndarray, max_lag: int) -> np.ndarray:
    x = x - x.mean()
    denom = float(np.dot(x, x))
    if denom == 0:
        return np.zeros(max_lag)
    return np.array([np.dot(x[:-l], x[l:]) / denom for l in range(1, max_lag + 1)])


def effective_n(x: np.ndarray, y: np.ndarray) -> float:
    """N_eff = N / (1 + 2·Σ ρx(l)·ρy(l))."""
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 4:
        return float(n)
    L = max(1, n // 3)
    s = float(np.sum(_acf(x, L) * _acf(y, L)))
    return float(np.clip(n / (1 + 2 * s), 3.0, n))


def r_pvalue(r: float, n_eff: float) -> float:
    """Двусторонний p для r при эффективном N (t-распределение)."""
    if not np.isfinite(r) or n_eff <= 2.5 or abs(r) >= 1:
        return np.nan
    t = r * np.sqrt((n_eff - 2) / (1 - r * r))
    return float(2 * stats.t.sf(abs(t), df=n_eff - 2))


# --------------------------------------------------- циркулярный суррогат ---

def _window_arrays(pairs: pd.DataFrame, mode: str) -> list[tuple[np.ndarray, np.ndarray]]:
    from .slices import MODE_COLS
    cx, cy = MODE_COLS.get(mode, MODE_COLS["diffs"])
    return [(g[cx].to_numpy(), g[cy].to_numpy())
            for _, g in pairs.groupby("window", sort=False)]


def _pooled_lag_r(wins: list[tuple[np.ndarray, np.ndarray]], ks: np.ndarray) -> np.ndarray:
    rs = np.full(len(ks), np.nan)
    for i, k in enumerate(ks):
        xs, ys = [], []
        for x, y in wins:
            if k >= 0:
                xa, ya = (x[:-k] if k else x), y[k:]
            else:
                xa, ya = x[-k:], y[:k]
            xs.append(xa); ys.append(ya)
        xc, yc = np.concatenate(xs), np.concatenate(ys)
        m = np.isfinite(xc) & np.isfinite(yc)
        if m.sum() >= MIN_PAIRS and xc[m].std() > 0 and yc[m].std() > 0:
            rs[i] = np.corrcoef(xc[m], yc[m])[0, 1]
    return rs


def circular_null(pairs: pd.DataFrame, mode: str, ks: list[int],
                  n_iter: int = 3000, seed: int = 42) -> dict:
    """Распределение r(k) и max|r| при случайном циркулярном сдвиге ряда CH
    в каждом окне (автокорреляция сохранена, совместное выравнивание разрушено).
    """
    rng = np.random.default_rng(seed)
    ks = np.asarray(ks)
    wins = _window_arrays(pairs, mode)

    max_abs = []
    per_lag = np.full((n_iter, len(ks)), np.nan)
    for it in range(n_iter):
        shuffled = []
        for x, y in wins:
            off = int(rng.integers(1, len(x)))
            shuffled.append((np.roll(x, off), y))
        rs = _pooled_lag_r(shuffled, ks)
        per_lag[it] = np.abs(rs)
        if np.isfinite(rs).any():
            max_abs.append(float(np.nanmax(np.abs(rs))))

    q95 = {int(k): float(np.nanquantile(per_lag[:, i], 0.95))
           for i, k in enumerate(ks)}
    return {"max_abs": np.array(max_abs), "per_lag_q95": q95}


def null_pvalue(observed_max_abs_r: float, null_max_abs: np.ndarray) -> float:
    """P(max|r| суррогатов ≥ наблюдаемого) — честный p всего лаг-скана."""
    if len(null_max_abs) == 0 or not np.isfinite(observed_max_abs_r):
        return np.nan
    return float((1 + np.sum(null_max_abs >= observed_max_abs_r)) / (1 + len(null_max_abs)))


# ------------------------------------------------------ блочный бутстрэп ----

def block_bootstrap_ci(x: np.ndarray, y: np.ndarray, block: int = 5,
                       n_iter: int = 4000, seed: int = 42,
                       ci: float = 0.90) -> tuple[float, float]:
    """CI для r(x,y) методом движущихся блоков (пары сохраняются)."""
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < block + 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    starts_max = n - block
    n_blocks = int(np.ceil(n / block))
    rs = []
    for _ in range(n_iter):
        starts = rng.integers(0, starts_max + 1, n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        xb, yb = x[idx], y[idx]
        if xb.std() == 0 or yb.std() == 0:
            continue
        rs.append(np.corrcoef(xb, yb)[0, 1])
    lo, hi = np.quantile(rs, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return float(lo), float(hi)
