"""Торговый тест от горбов скорости солнечного ветра (SW speed by SDO 211A).

Идея пользователя: скорость СВ ходит «горбами» (дно/плато → разгон → вершина →
спад), и модель SW-211 — прогнозная: она показывает скорость у Земли на
FORECAST_LEAD ≈ 4 дня вперёд (дыра видна на диске за 3–4 дня до прихода
потока). Правила:
  LONG  — со дна: вход за 1–2 дня ДО дна или в первые дни разгона;
  SHORT — с верха горба: вход у вершины, на спад.

Причинность при прогнозной модели: стоя в дне t, трейдер знает кривую до
t+FORECAST_LEAD. Экстремум в точке te опознаётся, когда видно разворот —
через CONFIRM_RUN шагов после te. Значит, раньше всего войти можно в день
te + CONFIRM_RUN − FORECAST_LEAD (при 4-дневной форе — за 2 дня до экстремума).
Для фактической скорости (Bulk) форы нет: там causal-вход только с te+CONFIRM_RUN.

Каждая клетка (d, h) сравнивается с теми же входами в случайные даты (тот же
период, то же число сделок, то же направление) — это снимает эффект общего
дрейфа рынка. При ~60 клетках сетки ~3 клетки с p<0.05 ожидаются даже у чистого
шума — смотреть надо на устойчивые области, а не на одинокие звёздочки.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal

SMOOTH_D = 3            # сглаживание скорости перед поиском структуры, дней
PROMINENCE = 60.0       # км/с — «горб» должен быть заметным
MIN_DIST_D = 7          # минимум дней между экстремумами
CONFIRM_KMS = 25.0      # подтверждение: ушли от экстремума на столько км/с
CONFIRM_RUN = 2         # ... двумя движениями подряд
FORECAST_LEAD = 4       # модель SW-211 показывает ~на столько дней вперёд
DELAYS = (-2, -1, 0, 1, 2, 3)   # дни входа относительно ЭКСТРЕМУМА
HOLDS = (2, 3, 5, 7, 10)


def find_events(sw: pd.DataFrame, col: str = "sw211_kms") -> pd.DataFrame:
    """Экстремумы скорости, подтверждённые разворотом (для отбора настоящих
    горбов; момент «увиденности» экстремума = te + CONFIRM_RUN − фора)."""
    d = sw.dropna(subset=[col]).reset_index(drop=True)
    v = d[col].rolling(SMOOTH_D, center=True, min_periods=2).mean().to_numpy()
    rows = []
    for kind, sgn in (("trough", +1.0), ("peak", -1.0)):
        arr = -v if kind == "trough" else v
        idx, _ = signal.find_peaks(arr, prominence=PROMINENCE, distance=MIN_DIST_D)
        for i in idx:
            run, conf = 0, None
            for j in range(i + 1, min(i + 12, len(v))):
                moved = (v[j] - v[i]) * sgn
                run = run + 1 if (v[j] - v[j - 1]) * sgn > 0 else 0
                if moved >= CONFIRM_KMS and run >= CONFIRM_RUN:
                    conf = j
                    break
            if conf is None:
                continue
            rows.append({"kind": kind, "extremum": d.loc[i, "date"],
                         "confirmed": d.loc[conf, "date"],
                         "v_ext": float(v[i]), "v_conf": float(v[conf])})
    return pd.DataFrame(rows).sort_values("extremum").reset_index(drop=True)


def backtest(events: pd.DataFrame, btc: pd.DataFrame, forecast_lead: int = FORECAST_LEAD,
             n_boot: int = 3000, seed: int = 42) -> pd.DataFrame:
    """Сетка (тип, d, h): средняя сделка, hit-rate, p против случайных дат.

    Клетки, невозможные причинно (вход раньше, чем разворот виден даже с
    учётом форы прогноза), пропускаются.
    """
    earliest = CONFIRM_RUN - forecast_lead  # раньше этого дня от te входа нет
    px = btc.set_index("date")["close_usd"]
    px = px.reindex(pd.date_range(px.index.min(), px.index.max())).ffill(limit=2)
    logp = np.log(px)
    rng = np.random.default_rng(seed)
    all_days = logp.dropna().index
    horizon_ok = {h: all_days[all_days + pd.Timedelta(days=h) <= all_days.max()]
                  for h in HOLDS}

    rows = []
    for kind, sgn in (("trough", +1.0), ("peak", -1.0)):
        ev = events[events["kind"] == kind]
        for dly in DELAYS:
            if dly < earliest:
                continue
            for h in HOLDS:
                rets = []
                for _, e in ev.iterrows():
                    t_in = e["extremum"] + pd.Timedelta(days=dly)
                    t_out = t_in + pd.Timedelta(days=h)
                    if t_in in logp.index and t_out in logp.index \
                            and np.isfinite(logp[t_in]) and np.isfinite(logp[t_out]):
                        rets.append(sgn * float(logp[t_out] - logp[t_in]))
                n = len(rets)
                if n < 5:
                    continue
                mean_r = float(np.mean(rets))
                hit = float(np.mean(np.array(rets) > 0))
                pool = horizon_ok[h]
                boot = np.empty(n_boot)
                for b in range(n_boot):
                    days = pool[rng.integers(0, len(pool), n)]
                    rr = (logp[days + pd.Timedelta(days=h)].to_numpy()
                          - logp[days].to_numpy())
                    boot[b] = sgn * np.nanmean(rr)
                p_rand = float((1 + np.sum(boot >= mean_r)) / (1 + n_boot))
                rows.append({"kind": kind, "delay": dly, "hold": h, "n": n,
                             "mean_pct": 100 * mean_r, "hit": hit,
                             "rand_mean_pct": 100 * float(np.mean(boot)),
                             "excess_pct": 100 * (mean_r - float(np.mean(boot))),
                             "p_rand": p_rand})
    return pd.DataFrame(rows)
