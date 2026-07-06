"""Разрез «с растяжением времени»: CH-ряд растягивается/сжимается в s раз,
сдвигается на произвольный якорь и сравнивается с дневным BTC.

Это формализация наложения «N дней СДО на месяцы рынка»: масштаб s означает,
что 1 день солнечного ряда занимает s дней рыночного. Вертикальный масштаб
Пирсонова корреляция прощает сама (она аффинно-инвариантна), так что здесь
перебираются ровно те свободы, которые есть у руки с графиком: s и сдвиг.

Честный вопрос к такому перебору: даёт ли реальный CH-ряд лучшее совпадение,
чем его же случайно прокрученная копия с теми же свободами перебора?
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("2025-01-01")


def to_days(ts: pd.Series) -> np.ndarray:
    return ((ts - EPOCH) / pd.Timedelta(days=1)).to_numpy(dtype=float)


def hourly_arrays(hourly: pd.DataFrame, col: str = "ch211") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(t_дни, значения, индикатор реальных данных) на общей почасовой сетке."""
    h = hourly.dropna(subset=["dt"]).sort_values("dt")
    t = to_days(h["dt"])
    v = h[col].to_numpy(dtype=float)
    ok = np.isfinite(v).astype(float)
    # для интерполяции значений заполним дыры линейно, но валидность учитываем отдельно
    vi = pd.Series(v).interpolate(limit_area="inside").to_numpy()
    m = np.isfinite(vi)  # обрезаем ведущие/замыкающие N/A
    return t[m], vi[m], ok[m]


def _corr_cols(m: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Корреляция каждого столбца m с y (NaN-безопасно по столбцам целиком)."""
    mc = m - m.mean(axis=0)
    yc = y - y.mean()
    denom = np.sqrt((mc**2).sum(axis=0) * (yc**2).sum())
    with np.errstate(invalid="ignore", divide="ignore"):
        return (mc * yc[:, None]).sum(axis=0) / denom


def scale_scan(ch_t: np.ndarray, ch_v: np.ndarray, ch_ok: np.ndarray,
               btc_dates: pd.Series, btc_close: np.ndarray,
               scales: list[float], n_offsets: int = 80,
               min_coverage: float = 0.85) -> dict:
    """max|r| по сдвигам для каждого масштаба s.

    Возвращает по каждому s: r лучшего сдвига, его якорь (дата начала куска
    CH-ряда) и использованную длину куска в днях CH-времени.
    """
    bd = to_days(btc_dates)
    bspan = bd - bd[0]
    y = np.log(btc_close)
    t0, t1 = ch_t.min(), ch_t.max()

    per_scale = []
    for s in scales:
        need = bspan[-1] / s                      # длина куска CH-времени, дней
        lo, hi = t0, t1 - need
        if hi <= lo:
            per_scale.append({"s": s, "r": np.nan, "anchor": None, "len_days": need})
            continue
        anchors = np.linspace(lo, hi, n_offsets)
        # матрица: строки — дни BTC, столбцы — варианты якоря
        grid = anchors[None, :] + bspan[:, None] / s
        vals = np.interp(grid, ch_t, ch_v)
        cover = np.interp(grid, ch_t, ch_ok)
        rs = _corr_cols(vals, y)
        rs[cover.mean(axis=0) < min_coverage] = np.nan  # кусок попал в дыру данных
        if np.isfinite(rs).any():
            i = int(np.nanargmax(np.abs(rs)))
            per_scale.append({
                "s": s, "r": float(rs[i]),
                "anchor": EPOCH + pd.Timedelta(days=float(anchors[i])),
                "len_days": float(need),
                "stretched": vals[:, i],
            })
        else:
            per_scale.append({"s": s, "r": np.nan, "anchor": None, "len_days": need})

    finite = [p for p in per_scale if np.isfinite(p["r"])]
    best = max(finite, key=lambda p: abs(p["r"])) if finite else None
    return {"per_scale": per_scale, "best": best}


def scale_scan_null(ch_t: np.ndarray, ch_v: np.ndarray, ch_ok: np.ndarray,
                    btc_dates: pd.Series, btc_close: np.ndarray,
                    scales: list[float], n_iter: int = 300,
                    n_offsets: int = 80, seed: int = 42) -> dict:
    """Суррогатный нуль: CH-ряд циркулярно прокручивается, скан повторяется.
    Возвращает 95-процентили max|r| по каждому масштабу и распределение
    глобального max|r| (по всем масштабам и сдвигам сразу)."""
    rng = np.random.default_rng(seed)
    n = len(ch_v)
    per_scale_max = {s: [] for s in scales}
    global_max = []
    for _ in range(n_iter):
        off = int(rng.integers(1, n))
        res = scale_scan(ch_t, np.roll(ch_v, off), np.roll(ch_ok, off),
                         btc_dates, btc_close, scales, n_offsets=n_offsets)
        gmax = -np.inf
        for p in res["per_scale"]:
            if np.isfinite(p["r"]):
                per_scale_max[p["s"]].append(abs(p["r"]))
                gmax = max(gmax, abs(p["r"]))
        if np.isfinite(gmax):
            global_max.append(gmax)
    q95 = {s: (float(np.quantile(v, 0.95)) if v else np.nan)
           for s, v in per_scale_max.items()}
    return {"per_scale_q95": q95, "global_max": np.array(global_max)}
