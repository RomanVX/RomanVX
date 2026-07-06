"""Экстраполяции «по-разному».

CH-площадь:
  E1 гармоническая модель кэррингтоновского вращения (P = 27.2753 сут,
     2 гармоники + тренд) — физически мотивирована рекуррентностью КД;
  E2 демпфированный Holt (statsmodels);
  E3 AR(2) по уровням.

BTC:
  E4 лаг-карта: btc_ret(t) = a + b·ΔCH(t−k*) на лучшем причинном лаге,
     будущие ΔCH берутся из хвоста наблюдений и гармонического прогноза;
     веер — бутстрэп остатков регрессии;
  E5 бейзлайны: случайное блуждание с дрейфом (блочный бутстрэп доходностей)
     и AR(2) по доходностям.

Всё это игровая экстраполяция для сравнения форм, не прогноз.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CARRINGTON_D = 27.2753


# ------------------------------------------------------------------ CH ------

def fit_harmonic(dates: pd.Series, y: np.ndarray, period: float = CARRINGTON_D,
                 n_harm: int = 2, trend: bool = True):
    """МНК-подгонка c + Σ_h [a_h sin + b_h cos] (+ d·t). Возвращает predict(dates)."""
    t0 = dates.min()
    t = (dates - t0).dt.days.to_numpy(dtype=float)

    def design(tt: np.ndarray) -> np.ndarray:
        cols = [np.ones_like(tt)]
        for h in range(1, n_harm + 1):
            w = 2 * np.pi * h / period
            cols += [np.sin(w * tt), np.cos(w * tt)]
        if trend:
            cols.append(tt / 100.0)
        return np.column_stack(cols)

    X = design(t)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef

    def predict(new_dates: pd.Series) -> np.ndarray:
        tt = (pd.Series(new_dates) - t0).dt.days.to_numpy(dtype=float)
        return design(tt) @ coef

    return predict, coef, float(np.sqrt(np.mean(resid**2)))


def holt_forecast(y: np.ndarray, horizon: int) -> np.ndarray:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    fit = ExponentialSmoothing(
        y, trend="add", damped_trend=True, initialization_method="estimated"
    ).fit(optimized=True)
    return np.asarray(fit.forecast(horizon))


def ar_forecast(y: np.ndarray, horizon: int, lags: int = 2) -> np.ndarray:
    from statsmodels.tsa.ar_model import AutoReg
    fit = AutoReg(y, lags=lags, old_names=False).fit()
    return np.asarray(fit.forecast(horizon))


def ch_forecasts(ch: pd.DataFrame, horizon: int = 35, floor: float = 0.5) -> dict:
    """Прогнозы CH тремя способами от последней даты наблюдений."""
    last = ch["date"].max()
    fdates = pd.Series(pd.date_range(last + pd.Timedelta(days=1), periods=horizon))

    pred_pooled, _, rmse_pooled = fit_harmonic(ch["date"], ch["ch211_pct"].to_numpy())
    jun = ch[ch["window"] == ch["window"].iloc[-1]]
    pred_jun, _, rmse_jun = fit_harmonic(jun["date"], jun["ch211_pct"].to_numpy(),
                                         n_harm=1, trend=True)

    out = {
        "dates": fdates,
        "harmonic_pooled": np.clip(pred_pooled(fdates), floor, None),
        "harmonic_recent": np.clip(pred_jun(fdates), floor, None),
        "holt": np.clip(holt_forecast(jun["ch211_pct"].to_numpy(), horizon), floor, None),
        "ar2": np.clip(ar_forecast(jun["ch211_pct"].to_numpy(), horizon), floor, None),
        "fit_pooled": (pred_pooled, rmse_pooled),
        "fit_recent": (pred_jun, rmse_jun),
    }
    return out


# ----------------------------------------------------------------- BTC ------

def lagmap_regression(pairs: pd.DataFrame, k: int) -> dict:
    """OLS btc_ret(t) = a + b·ΔCH(t−k) внутри окон; возвращает и остатки."""
    xs, ys = [], []
    for _, g in pairs.groupby("window"):
        x = g["dch"].to_numpy()
        y = g["btc_ret"].to_numpy()
        if k > 0:
            x, y = x[:-k], y[k:]
        elif k < 0:
            x, y = x[-k:], y[:k]
        m = np.isfinite(x) & np.isfinite(y)
        xs.append(x[m]); ys.append(y[m])
    x = np.concatenate(xs); y = np.concatenate(ys)
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    return {"a": float(coef[0]), "b": float(coef[1]), "resid": resid, "n": len(y)}


def btc_lagmap_fan(pairs: pd.DataFrame, ch: pd.DataFrame, ch_future: pd.Series,
                   future_dates: pd.Series, k: int, last_close: float,
                   n_paths: int = 2000, seed: int = 42) -> dict:
    """Веер траекторий BTC из лаг-регрессии + бутстрэпа остатков."""
    reg = lagmap_regression(pairs, k)
    rng = np.random.default_rng(seed)

    ch_all = pd.concat([
        ch[["date", "ch211_pct"]],
        pd.DataFrame({"date": future_dates, "ch211_pct": ch_future}),
    ]).set_index("date")["ch211_pct"]
    dch_all = ch_all.diff()

    horizon = len(future_dates)
    drivers = np.array([
        dch_all.get(d - pd.Timedelta(days=k), np.nan) for d in future_dates
    ])
    drivers = np.nan_to_num(drivers, nan=0.0)
    mean_ret = reg["a"] + reg["b"] * drivers

    paths = np.empty((n_paths, horizon))
    for i in range(n_paths):
        eps = rng.choice(reg["resid"], size=horizon, replace=True)
        paths[i] = last_close * np.exp(np.cumsum(mean_ret + eps))
    qs = np.quantile(paths, [0.05, 0.25, 0.50, 0.75, 0.95], axis=0)
    return {"quantiles": qs, "mean_ret": mean_ret, "reg": reg}


def btc_baselines(btc: pd.DataFrame, horizon: int, drift_win: int = 30,
                  n_paths: int = 2000, seed: int = 43) -> dict:
    """RW с дрейфом (блочный бутстрэп дневных доходностей) и AR(2)-медиана."""
    close = btc["close_usd"].to_numpy()
    rets = np.diff(np.log(close))
    tail = rets[-drift_win:]
    rng = np.random.default_rng(seed)

    block = 5
    paths = np.empty((n_paths, horizon))
    for i in range(n_paths):
        seq = []
        while len(seq) < horizon:
            s = rng.integers(0, len(tail) - block + 1)
            seq.extend(tail[s:s + block])
        paths[i] = close[-1] * np.exp(np.cumsum(seq[:horizon]))
    rw_q = np.quantile(paths, [0.05, 0.50, 0.95], axis=0)

    ar_med = close[-1] * np.exp(np.cumsum(ar_forecast(rets, horizon, lags=2)))
    return {"rw_quantiles": rw_q, "ar2_median": ar_med}
