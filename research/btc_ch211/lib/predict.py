"""Решение задачи «строим логику от воздействия Солнца»: честный прогнозный
тест (walk-forward, без подглядывания в будущее).

Каждый день t модель обучается ТОЛЬКО на данных до t и предсказывает доходность
BTC за день t+1. Сравниваются:

  SOLAR — гребневая регрессия на лагах солнечных признаков (ΔCH за последние
          12 дней + текущий z-уровень площади CH);
  AR    — та же регрессия, но на собственных лагах доходности BTC (Солнце слепое);
  ZERO  — «завтра ничего не изменится» (случайное блуждание).

Метрики: доля угаданных направлений (hit-rate) с биномиальным p против 50%,
skill = 1 − MSE/MSE_zero, и игрушечная кумулятивная кривая знаковой стратегии.
Если у Солнца есть предсказательная сила — она обязана проявиться здесь.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

N_DCH_LAGS = 12
N_AR_LAGS = 5
MIN_TRAIN = 120
RIDGE_ALPHA = 3.0


def build_design(pairs: pd.DataFrame) -> pd.DataFrame:
    """Строки: дни, для которых все лаги лежат внутри одного окна данных."""
    rows = []
    for _, g in pairs.groupby("window"):
        g = g.reset_index(drop=True)
        for i in range(max(N_DCH_LAGS, N_AR_LAGS), len(g) - 1):
            feat = {"date": g.loc[i, "date"], "y_next": g.loc[i + 1, "btc_ret"]}
            for k in range(N_DCH_LAGS):
                feat[f"dch_l{k}"] = g.loc[i - k, "dch"]
            feat["ch_z"] = g.loc[i, "ch_z"]
            for k in range(N_AR_LAGS):
                feat[f"ret_l{k}"] = g.loc[i - k, "btc_ret"]
            rows.append(feat)
    df = pd.DataFrame(rows).dropna().sort_values("date").reset_index(drop=True)
    return df


def _ridge_predict(X_tr: np.ndarray, y_tr: np.ndarray, x_te: np.ndarray,
                   alpha: float = RIDGE_ALPHA) -> float:
    mu, sd = X_tr.mean(axis=0), X_tr.std(axis=0)
    sd[sd == 0] = 1.0
    Xs = (X_tr - mu) / sd
    A = Xs.T @ Xs + alpha * np.eye(Xs.shape[1])
    b = np.linalg.solve(A, Xs.T @ (y_tr - y_tr.mean()))
    return float(y_tr.mean() + ((x_te - mu) / sd) @ b)


def walk_forward(design: pd.DataFrame) -> dict:
    solar_cols = [c for c in design.columns if c.startswith("dch_")] + ["ch_z"]
    ar_cols = [c for c in design.columns if c.startswith("ret_l")]
    y = design["y_next"].to_numpy()
    Xs_all = design[solar_cols].to_numpy()
    Xa_all = design[ar_cols].to_numpy()

    preds = {"SOLAR": [], "AR": [], "ZERO": []}
    idx = []
    for i in range(MIN_TRAIN, len(design)):
        preds["SOLAR"].append(_ridge_predict(Xs_all[:i], y[:i], Xs_all[i]))
        preds["AR"].append(_ridge_predict(Xa_all[:i], y[:i], Xa_all[i]))
        preds["ZERO"].append(0.0)
        idx.append(i)
    actual = y[idx]
    dates = design["date"].iloc[idx].reset_index(drop=True)

    out = {"dates": dates, "actual": actual, "models": {}}
    mse_zero = float(np.mean(actual**2))
    for name, p in preds.items():
        p = np.asarray(p)
        nz = actual != 0
        hits = int(np.sum(np.sign(p[nz]) == np.sign(actual[nz]))) if name != "ZERO" else 0
        n_dir = int(nz.sum())
        binom_p = float(stats.binomtest(hits, n_dir, 0.5).pvalue) if name != "ZERO" else np.nan
        out["models"][name] = {
            "pred": p,
            "hit_rate": hits / n_dir if name != "ZERO" else np.nan,
            "n": n_dir,
            "binom_p": binom_p,
            "skill": 1.0 - float(np.mean((actual - p) ** 2)) / mse_zero,
            "strategy_curve": np.cumsum(np.sign(p) * actual),
        }
    out["buy_hold"] = np.cumsum(actual)
    return out
