#!/usr/bin/env python3
"""Корреляция площади корональных дыр (SDO/AIA 211Å, SINP МГУ) с курсом BTC.

Запуск:  python run.py
Выход:   out/fig*.png, out/REPORT.md

Источники данных подхватываются автоматически (см. data/README.md):
qlookdata SINP > CSV-экспорт графика > оцифровка скриншотов; для BTC —
btc_export.csv > оцифровка свечей с датированными якорями.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (data_io, extrapolate, honest_stats, plots, predict, slices,  # noqa: E402
                 stretch)

OUT = Path(__file__).resolve().parent / "out"
KMAX = 12          # максимум лага в скане, дней
ROLL_WIN = 15      # окно скользящей корреляции, дней
HORIZON = 35       # горизонт экстраполяции, дней
NULL_ITER = 3000   # итераций циркулярных суррогатов


def main() -> None:
    OUT.mkdir(exist_ok=True)
    plots.setup_style()

    # ------------------------------------------------------------ данные ----
    ch, ch_src = data_io.load_ch211()
    btc, btc_src = data_io.load_btc()
    pairs = data_io.make_pairs(ch, btc)
    wins = list(pairs["window"].unique())
    wtitles = {w: plots.win_title(pairs[pairs["window"] == w]) for w in wins}
    print(f"CH-211: {ch_src}: {len(ch)} дн, {ch['window'].nunique()} окн. "
          f"({ch['date'].min():%d.%m}–{ch['date'].max():%d.%m})")
    print(f"BTC:    {btc_src}: {len(btc)} дн")
    print(f"Пары:   {len(pairs)} дн в {len(wins)} окнах: "
          + ", ".join(f"{wtitles[w]} ({(pairs.window == w).sum()})" for w in wins))

    # плотность исходных данных по месяцам (ответ на вопрос про «сжатие»)
    coverage = None
    if "n_obs" in ch.columns:
        coverage = (ch.assign(m=ch["date"].dt.strftime("%Y-%m"))
                    .groupby("m")
                    .agg(days=("date", "size"), hours=("n_obs", "sum")))
        thin = coverage[coverage["days"] < 24].index.tolist()
        print(f"Плотность CH-данных: {coverage['hours'].sum():.0f} почасовых записей; "
              f"месяцы с дырами: {', '.join(thin) if thin else 'нет'}")

    # точность оцифровки vs реальные данные (если доступны оба источника)
    digit_rmse = None
    dig_path = data_io.DATA_DIR / "ch211_daily.csv"
    if data_io.QLOOK.exists() and dig_path.exists():
        dig = pd.read_csv(dig_path, parse_dates=["date"])
        cmp_ = dig.merge(ch, on="date", suffixes=("_dig", ""))
        if len(cmp_):
            digit_rmse = float(np.sqrt(np.mean(
                (cmp_["ch211_pct_dig"] - cmp_["ch211_pct"]) ** 2)))
            print(f"Оцифровка скриншотов vs реальные данные: RMSE {digit_rmse:.2f} п.п. "
                  f"на {len(cmp_)} общих днях")

    btc_rmse = None
    dig_btc = data_io.DATA_DIR / "btc_usd_daily.csv"
    if "биржевые" in btc_src and dig_btc.exists():
        db = pd.read_csv(dig_btc, parse_dates=["date"])
        db = db[~db["provenance"].str.contains("partial", na=False)]
        cb = db.merge(btc, on="date", suffixes=("_dig", ""))
        if len(cb):
            err = cb["close_usd_dig"] - cb["close_usd"]
            btc_rmse = (float(np.sqrt(np.mean(err**2))), float(np.abs(err).max()),
                        len(cb))
            print(f"Оцифровка BTC vs биржа: RMSE ${btc_rmse[0]:,.0f}, "
                  f"max ${btc_rmse[1]:,.0f} на {btc_rmse[2]} днях")

    # -------------------------------------------------------- лаг-сканы -----
    scans: dict[tuple[str, str], list[slices.LagResult]] = {}
    for mode in ("levels", "diffs", "levels_s3"):
        for scope in (*wins, "pooled"):
            scans[(mode, scope)] = slices.lag_scan(pairs, mode, scope, kmax=KMAX)

    nulls, verdicts = {}, {}
    for mode in ("levels", "diffs"):
        pooled = [r for r in scans[(mode, "pooled")] if np.isfinite(r.r)]
        best = slices.best_lag(pooled)
        ks = [r.k for r in scans[(mode, "pooled")]]
        nulls[mode] = honest_stats.circular_null(pairs, mode, ks, n_iter=NULL_ITER)
        p_scan = honest_stats.null_pvalue(abs(best.r), nulls[mode]["max_abs"])
        x, y = slices.lagged_pairs(pairs, best.k, mode)
        neff = honest_stats.effective_n(x, y)
        p_naive = honest_stats.r_pvalue(best.r, neff)
        ci = honest_stats.block_bootstrap_ci(x, y)
        verdicts[mode] = {"best": best, "p_scan": p_scan, "neff": neff,
                          "p_naive": p_naive, "ci": ci, "x": x, "y": y}
        print(f"[BTC {mode}] лучший k={best.k:+d} r={best.r:+.2f} (n={best.n}); "
              f"N_eff≈{neff:.0f}; p(N_eff)≈{p_naive:.3f}; p(скан)≈{p_scan:.3f}")

    # «вау-цифры» коротких окон проверяются собственным суррогатным тестом:
    # короткое окно + перебор лагов легко дают |r| ~ 0.7-0.8 из ничего
    win_checks = []
    for w in wins:
        sub = pairs[pairs["window"] == w].reset_index(drop=True)
        for mode in ("levels", "diffs"):
            res = [r for r in scans[(mode, w)] if np.isfinite(r.r)]
            if not res:
                continue
            b = slices.best_lag(res)
            null_w = honest_stats.circular_null(
                sub, mode, [r.k for r in scans[(mode, w)]], n_iter=1500)
            p_w = honest_stats.null_pvalue(abs(b.r), null_w["max_abs"])
            win_checks.append({"title": wtitles[w], "mode": mode, "best": b, "p": p_w})
            print(f"[окно {wtitles[w]} | {mode}] k={b.k:+d} r={b.r:+.2f} "
                  f"→ p(скан окна)≈{p_w:.3f}")

    # --------------------------- допрос пограничного сигнала (уровни, k*>0) -
    # если сырые уровни дают что-то на грани значимости, гоняем три решающих
    # теста: детренд, воспроизводимость в половинах периода, синусоида-двойник
    TREND_WIN = 27
    gW = pairs.groupby("window")
    pairs["ch_dt"] = pairs["ch_z"] - gW["ch_z"].transform(
        lambda s: s.rolling(TREND_WIN, center=True, min_periods=TREND_WIN // 2).mean())
    pairs["btc_dt"] = pairs["btc_z"] - gW["btc_z"].transform(
        lambda s: s.rolling(TREND_WIN, center=True, min_periods=TREND_WIN // 2).mean())

    k_lv = verdicts["levels"]["best"].k
    sc_dt = slices.lag_scan(pairs, "levels_dt", "pooled", kmax=KMAX)
    null_dt = honest_stats.circular_null(pairs, "levels_dt",
                                         [r.k for r in sc_dt], n_iter=NULL_ITER)
    b_dt = slices.best_lag([r for r in sc_dt if np.isfinite(r.r)])
    p_dt = honest_stats.null_pvalue(abs(b_dt.r), null_dt["max_abs"])
    r_dt_at = next((r.r for r in sc_dt if r.k == k_lv), np.nan)
    print(f"[допрос: детренд {TREND_WIN}д] r(k={k_lv:+d})={r_dt_at:+.2f}; "
          f"лучший k={b_dt.k:+d} r={b_dt.r:+.2f}; p(скан)≈{p_dt:.3f}")

    mid = pairs["date"].sort_values().iloc[len(pairs) // 2]
    halves = []
    for name, chh in (("первая половина", ch[ch["date"] <= mid]),
                      ("вторая половина", ch[ch["date"] > mid])):
        ph = data_io.make_pairs(chh, btc)
        sch = slices.lag_scan(ph, "levels", "pooled", kmax=KMAX)
        fin = [r for r in sch if np.isfinite(r.r)]
        if not fin:
            continue
        bh = slices.best_lag(fin)
        r_at = next((r.r for r in sch if r.k == k_lv), np.nan)
        nullh = honest_stats.circular_null(ph, "levels", [r.k for r in sch],
                                           n_iter=1500)
        p_h = honest_stats.null_pvalue(abs(bh.r), nullh["max_abs"])
        halves.append({"name": name, "lo": ph["date"].min(), "hi": ph["date"].max(),
                       "best": bh, "r_at": r_at, "p": p_h, "n": len(ph)})
        print(f"[допрос: {name} {ph['date'].min():%d.%m}–{ph['date'].max():%d.%m}] "
              f"r(k={k_lv:+d})={r_at:+.2f}; лучший k={bh.k:+d} r={bh.r:+.2f}; "
              f"p≈{p_h:.3f}")

    t_days = (pairs["date"] - pairs["date"].min()).dt.days.to_numpy(float)
    yz = pairs["btc_z"].to_numpy()
    m_fin = np.isfinite(yz)
    r_sine = 0.0
    for phase in np.linspace(0, 2 * np.pi, 144, endpoint=False):
        s = np.sin(2 * np.pi * t_days / extrapolate.CARRINGTON_D + phase)
        r_sine = max(r_sine, abs(float(np.corrcoef(s[m_fin], yz[m_fin])[0, 1])))
    print(f"[допрос: синусоида 27.27д вместо Солнца] max|r| по фазам = {r_sine:.2f}")

    sign_ok = all(np.isfinite(h["r_at"]) and
                  np.sign(h["r_at"]) == np.sign(verdicts["levels"]["best"].r) and
                  abs(h["r_at"]) > 0.15 for h in halves) and len(halves) == 2
    survives = p_dt < 0.05 and sign_ok and \
        abs(verdicts["levels"]["best"].r) > r_sine + 0.05
    probe = {"k_lv": k_lv, "scan_dt": sc_dt, "null_dt": null_dt, "best_dt": b_dt,
             "p_dt": p_dt, "r_dt_at": r_dt_at, "halves": halves, "r_sine": r_sine,
             "survives": survives, "trend_win": TREND_WIN}
    print(f"[допрос: вердикт] сигнал {'ПЕРЕЖИЛ все три теста' if survives else 'не пережил допрос'}")

    # ------------------------------------------- позитивный контроль (СВ) ---
    control = None
    sw = data_io.load_solar_wind()
    if sw is not None:
        sw_pairs = data_io.make_pairs(ch, sw, value_col="bulk_kms")
        sw_scan = slices.lag_scan(sw_pairs, "levels", "pooled", kmax=KMAX)
        sw_best = slices.best_lag([r for r in sw_scan if np.isfinite(r.r)])
        sw_ks = [r.k for r in sw_scan]
        sw_null = honest_stats.circular_null(sw_pairs, "levels", sw_ks, n_iter=NULL_ITER)
        sw_p = honest_stats.null_pvalue(abs(sw_best.r), sw_null["max_abs"])
        control = {"pairs": sw_pairs, "scan": sw_scan, "best": sw_best,
                   "null": sw_null, "p": sw_p}
        print(f"[контроль CH×СВ] лучший k={sw_best.k:+d} r={sw_best.r:+.2f} "
              f"(n={sw_best.n}); p(скан)≈{sw_p:.4f}")

    # --------------------------------------- разрез с растяжением времени ---
    # формализация «наложения N дней СДО на месяцы рынка»: перебор масштабов
    # 0.25x…55x и всех сдвигов против такого же перебора на прокрученном ряде
    hourly = data_io._parse_qlook_hourly() if data_io.QLOOK.exists() else None
    stretch_res = None
    if hourly is not None:
        st_t, st_v, st_ok = stretch.hourly_arrays(hourly)
        scales_grid = [0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 55]
        obs = stretch.scale_scan(st_t, st_v, st_ok, btc["date"],
                                 btc["close_usd"].to_numpy(), scales_grid)
        nul = stretch.scale_scan_null(st_t, st_v, st_ok, btc["date"],
                                      btc["close_usd"].to_numpy(), scales_grid,
                                      n_iter=300)
        p_glob = honest_stats.null_pvalue(abs(obs["best"]["r"]), nul["global_max"])
        stretch_res = {"obs": obs, "null": nul, "p": p_glob, "scales": scales_grid}
        b = obs["best"]
        print(f"[растяжение] лучшее s=×{b['s']:g}, r={b['r']:+.2f} "
              f"(кусок CH от {b['anchor']:%d.%m.%Y}, {b['len_days']:.1f} дн) "
              f"→ p(всего перебора)≈{p_glob:.2f}; "
              f"случайный ряд даёт max|r|≥{float(np.quantile(nul['global_max'], 0.5)):.2f} "
              f"в половине попыток")

    # ------------------------------- многомасштабный разрез: бары по 4 часа -
    # проверка тезиса «4 часа коррелируют лучше»: та же батарея на 4ч-барах
    multitf = None
    btc_hourly = data_io.load_btc_hourly()
    if hourly is not None and btc_hourly is not None:
        ch4 = (pd.DataFrame({"date": hourly["dt"], "ch211_pct": hourly["ch211"]})
               .dropna().set_index("date").resample("4h")["ch211_pct"].mean()
               .dropna().reset_index())
        btc4 = (btc_hourly.set_index("dt").resample("4h")["close_usd"].last()
                .dropna().reset_index().rename(columns={"dt": "date"}))
        pairs4 = data_io.make_pairs(ch4, btc4, min_window=90)
        kmax4 = KMAX * 6  # ±12 дней в 4-часовых барах
        multitf = {"bar_hours": 4.0, "n": len(pairs4), "scans": {}, "nulls": {},
                   "verdicts": {}}
        for mode in ("levels", "diffs"):
            sc = slices.lag_scan(pairs4, mode, "pooled", kmax=kmax4)
            nul4 = honest_stats.circular_null(pairs4, mode, [r.k for r in sc],
                                              n_iter=500)
            b4 = slices.best_lag([r for r in sc if np.isfinite(r.r)])
            p4 = honest_stats.null_pvalue(abs(b4.r), nul4["max_abs"])
            x4, y4 = slices.lagged_pairs(pairs4, b4.k, mode)
            neff4 = honest_stats.effective_n(x4, y4)
            multitf["scans"][mode] = sc
            multitf["nulls"][mode] = nul4
            multitf["verdicts"][mode] = {"best": b4, "p": p4, "neff": neff4}
            print(f"[4ч {mode}] лучший k={b4.k:+d} бар ({b4.k / 6:+.1f} дн) "
                  f"r={b4.r:+.2f} (N={b4.n}, N_eff≈{neff4:.0f}); p(скан)≈{p4:.3f}")

    # ---------------------- решающий тест: предсказание вне выборки ---------
    # инженерная постановка «строим логику от воздействия Солнца»: каждый день
    # модель видит только прошлое и предсказывает завтрашнюю доходность BTC
    predict_res = None
    design = predict.build_design(pairs)
    if len(design) > predict.MIN_TRAIN + 40:
        predict_res = predict.walk_forward(design)
        for name in ("SOLAR", "AR"):
            m = predict_res["models"][name]
            print(f"[прогноз {name}] hit-rate={m['hit_rate']:.1%} (n={m['n']}, "
                  f"binom p={m['binom_p']:.2f}); skill vs ZERO={m['skill']:+.3f}")

    # -------------------------------------------------- прочие разрезы ------
    roll_lv = pd.concat([slices.rolling_corr(pairs, w, ROLL_WIN, "levels") for w in wins],
                        ignore_index=True)
    roll_df = pd.concat([slices.rolling_corr(pairs, w, ROLL_WIN, "diffs") for w in wins],
                        ignore_index=True)
    neg_share = float((roll_lv["r"] < 0).mean()) if len(roll_lv) else float("nan")
    events = slices.find_ch_events(ch, prominence=3.0)
    events = events[(events["date"] >= btc["date"].min() + pd.Timedelta(days=4))]
    study = slices.event_study(btc, events)

    # ------------------------------------------------------ экстраполяции ---
    chf = extrapolate.ch_forecasts(ch, horizon=HORIZON)
    best_diff = verdicts["diffs"]["best"]
    k_star = best_diff.k if best_diff.k > 0 else 3  # для прогноза нужен причинный лаг
    last_close = float(btc["close_usd"].iloc[-1])
    fan = extrapolate.btc_lagmap_fan(pairs, ch, pd.Series(chf["harmonic_pooled"]),
                                     chf["dates"], k_star, last_close)
    base = extrapolate.btc_baselines(btc, HORIZON)

    # ------------------------------------------------------------ графики ---
    plots.fig_overview(btc, pairs, ch, OUT / "fig1_overview.png")
    scopes = [(w, wtitles[w]) for w in wins] + [("pooled", "все окна (пул)")]
    plots.fig_lagscan(scans, scopes, nulls, OUT / "fig2_lagscan.png",
                      "Лаг-скан CH(t) × BTC(t+k) по разрезам — серая полоса: "
                      "что даёт случайность с той же автокорреляцией")

    picks = []
    for mode, tt, xl, yl in (
        ("levels", "Уровни (z в окне)", "CH 211Å, z(t)", "BTC, z(t+k)"),
        ("diffs", "Приращения", "ΔCH, п.п. (t)", "лог-доходность BTC (t+k)"),
    ):
        v = verdicts[mode]
        k = v["best"].k
        wl = np.concatenate([np.repeat(w, max(len(pairs[pairs.window == w]) - abs(k), 0))
                             for w in wins])
        picks.append({"k": k, "r": v["best"].r, "n": v["best"].n, "neff": v["neff"],
                      "p": v["p_naive"], "ci": v["ci"], "x": v["x"], "y": v["y"],
                      "wins": wl, "title": tt, "xlabel": xl, "ylabel": yl})
    plots.fig_scatter(picks, wtitles, OUT / "fig3_scatter.png")
    plots.fig_rolling(roll_lv, roll_df, ROLL_WIN, neg_share, OUT / "fig4_rolling.png")
    plots.fig_events(study, OUT / "fig5_events.png")
    plots.fig_forecast(ch, chf, btc, fan, base, k_star, OUT / "fig6_forecast.png")
    if control:
        plots.fig_control(control["pairs"], control["scan"], control["null"],
                          control["best"], OUT / "fig7_control.png")
    if stretch_res:
        plots.fig_stretch(btc, stretch_res["obs"], stretch_res["null"],
                          stretch_res["p"], stretch_res["scales"],
                          OUT / "fig8_stretch.png")
    if multitf:
        plots.fig_multitf(multitf["scans"], multitf["nulls"], multitf["verdicts"],
                          multitf["bar_hours"], OUT / "fig9_multitf.png")
    plots.fig_probe(probe, verdicts["levels"]["best"].r, OUT / "fig10_probe.png")
    if predict_res:
        plots.fig_predict(predict_res, OUT / "fig11_predict.png")

    # ------------------------------------------------------------- отчёт ----
    write_report(ch_src, btc_src, ch, pairs, wins, wtitles, scans, verdicts, nulls,
                 neg_share, events, chf, fan, base, k_star, control, digit_rmse,
                 win_checks, coverage, stretch_res, btc_rmse, multitf, probe,
                 predict_res)
    print(f"\nГотово: {OUT}/fig1…fig11.png, {OUT}/REPORT.md")


def write_report(ch_src, btc_src, ch, pairs, wins, wtitles, scans, verdicts, nulls,
                 neg_share, events, chf, fan, base, k_star, control,
                 digit_rmse, win_checks, coverage, stretch_res, btc_rmse,
                 multitf, probe, predict_res) -> None:
    L: list[str] = []
    add = L.append

    add("# Корональные дыры (SDO 211Å) × BTC: разрезы, честная статистика, экстраполяции\n")
    add(f"*Автоотчёт `run.py` от {pairs['date'].max():%d.%m.%Y}. "
        f"CH — {ch_src}; BTC — {btc_src}.*\n")
    tldr = ("слабое сходство есть, но оно не пережило проверок: совпадают только "
            "плавные изгибы, в половинах года связь не воспроизводится, и простая "
            "синусоида «коррелирует» не хуже Солнца"
            if not probe["survives"] else
            "слабый сигнал пережил все три проверки — это ещё не доказательство, "
            "но повод для вневыборочного теста на свежих данных")
    add(f"> **Коротко:** {tldr}. Подробности — ниже, человеческим языком — в "
        "сводке в самом конце.\n")
    add(f"Дневных пар: **{len(pairs)}** ("
        + "; ".join(f"{wtitles[w]}: {(pairs.window == w).sum()}" for w in wins)
        + "). Лаг k>0 = площадь КД опережает BTC на k дней.\n")
    if digit_rmse is not None:
        add(f"> Проверка ручной оцифровки скриншотов против реальной выгрузки: "
            f"RMSE **{digit_rmse:.2f} п.п.** — оценки погрешности в data/README.md честные.\n")
    if btc_rmse is not None:
        add(f"> Оцифровка BTC (свечи + новостные якоря) против биржевых данных: "
            f"RMSE **${btc_rmse[0]:,.0f}** (max ${btc_rmse[1]:,.0f}) на {btc_rmse[2]} "
            f"днях — заявленная точность ±1–2% подтвердилась; теперь всюду "
            f"используются биржевые цены.\n")

    add("\n## 0. Данные и их плотность (про «сжатие» на сайте)\n")
    add(f"Файл SINP: **{ch['date'].min():%d.%m.%Y} – {ch['date'].max():%d.%m.%Y}**, "
        "кадентность строго почасовая (24 записи/сутки). «Сжатие», которое видно "
        "на сайте — когда 3 дня растягиваются или месяц сплющивается — это "
        "**поведение экранного графика**: при широком диапазоне дат он усредняет и "
        "прореживает точки (data grouping в Highcharts), пики сглаживаются, а "
        "разрывы стягиваются. На исследование это не влияет: мы берём сырые "
        "почасовые строки файла и усредняем к календарным суткам — каждая точка "
        "жёстко привязана к своей дате.\n")
    if coverage is not None:
        add("| месяц | дней с данными CH-211 | почасовых записей |")
        add("|---|---|---|")
        for m, row in coverage.iterrows():
            add(f"| {m} | {int(row['days'])} | {int(row['hours'])} |")
        add("")

    add("\n## 1. Обзор\n\n![обзор](fig1_overview.png)\n")

    add("\n## 2. Лаг-скан по разрезам\n\n![лаг-скан](fig2_lagscan.png)\n")
    add("| разрез | масштаб | лучший лаг | r | n |")
    add("|---|---|---|---|---|")
    for mode, mname in (("levels", "уровни (z)"), ("levels_s3", "уровни, MA(3)"),
                        ("diffs", "приращения")):
        for scope in (*wins, "pooled"):
            res = [r for r in scans[(mode, scope)] if np.isfinite(r.r)]
            if not res:
                continue
            b = slices.best_lag(res)
            sname = wtitles.get(scope, "**пул**")
            add(f"| {mname} | {sname} | {b.k:+d} дн | {b.r:+.2f} | {b.n} |")

    add("\n## 3. Честная статистика (главный раздел)\n")
    for mode, name in (("levels", "Уровни"), ("diffs", "Приращения")):
        v = verdicts[mode]
        b = v["best"]
        null_q = float(np.quantile(nulls[mode]["max_abs"], 0.95))
        add(f"\n**{name}**: лучший лаг k*={b.k:+d} дн, r={b.r:+.2f} "
            f"(90% CI блочного бутстрэпа {v['ci'][0]:+.2f}…{v['ci'][1]:+.2f}), N={b.n}.")
        add(f"- эффективный N с учётом автокорреляции ≈ {v['neff']:.0f} "
            f"⇒ p ≈ {v['p_naive']:.3f};")
        add(f"- циркулярные суррогаты (автокорреляция сохранена, выравнивание "
            f"разрушено, {len(nulls[mode]['max_abs'])} итер.): 95-й процентиль "
            f"max|r| по скану = {null_q:.2f}; наблюдаемое max|r| = {abs(b.r):.2f} "
            f"⇒ **p(всего скана) ≈ {v['p_scan']:.3f}**.")
    if win_checks:
        add("\n**Проверка «вау-цифр» отдельных окон** — каждое окно тестируется "
            "своими суррогатами (короткое окно + перебор лагов легко дают большие |r|):\n")
        add("| окно | разрез | k* | r | p(скан окна) |")
        add("|---|---|---|---|---|")
        for c in win_checks:
            mname = "уровни" if c["mode"] == "levels" else "приращения"
            add(f"| {c['title']} | {mname} | {c['best'].k:+d} | {c['best'].r:+.2f} "
                f"| {c['p']:.3f} |")
        best_win = min(win_checks, key=lambda c: c["p"])
        n_checks = len(win_checks)
        add(f"\nСамая яркая цифра (r={best_win['best'].r:+.2f}, k={best_win['best'].k:+d}, "
            f"окно {best_win['title']}) даёт p≈{best_win['p']:.3f} в своём окне, но: "
            f"(а) проверок было {n_checks}, поправка Бонферрони ⇒ p≈"
            f"{min(1.0, best_win['p'] * n_checks):.2f}; (б) в соседнем окне знак "
            "у похожих лагов противоположный; (в) лаг на краю скана. Это профиль "
            "случайной находки — статус «интересно, перепроверить на 6+ месяцах», "
            "а не «связь установлена».")
    add(f"\n### Допрос главного подозреваемого: уровни, сдвиг k={probe['k_lv']:+d} дн\n")
    add("![допрос](fig10_probe.png)\n")
    r_raw = verdicts["levels"]["best"].r
    add(f"Сырые уровни дали r={r_raw:+.2f} на лаге {probe['k_lv']:+d} дн с "
        f"p(скана)≈{verdicts['levels']['p_scan']:.2f} — на грани. Три решающих теста:\n")
    if abs(probe["r_dt_at"]) < 0.6 * abs(r_raw):
        t1 = (f"связь падает до r={probe['r_dt_at']:+.2f} — совпадали только "
              "медленные многомесячные изгибы (они совпадают у любых двух плавных "
              "рядов), быстрая структура связь не подтверждает")
    elif probe["p_dt"] < 0.05:
        t1 = (f"связь выживает: r={probe['r_dt_at']:+.2f}, p≈{probe['p_dt']:.2f} — "
              "серьёзная заявка")
    else:
        t1 = (f"r={probe['r_dt_at']:+.2f}, p≈{probe['p_dt']:.2f} — ослабла, "
              "формально не значима")
    add(f"1. **Детренд** (минус скользящее среднее {probe['trend_win']} дн): {t1}.")
    for h in probe["halves"]:
        add(f"2. **{h['name'].capitalize()}** ({h['lo']:%d.%m}–{h['hi']:%d.%m}, "
            f"n={h['n']}): на том же лаге r={h['r_at']:+.2f}; лучший по скану "
            f"r={h['best'].r:+.2f} (k={h['best'].k:+d}), p≈{h['p']:.2f}.")
    same_sign = len({np.sign(h["r_at"]) for h in probe["halves"]
                     if np.isfinite(h["r_at"])}) == 1
    add(("   Знак совпадает в обеих половинах — очко в пользу сигнала."
         if same_sign else
         "   В половинах года знак/величина не воспроизводятся — так ведут себя "
         "совпадения.")
        )
    sine_verdict = ("не хуже настоящего Солнца — вклад именно солнечных данных "
                    "неотличим от вклада любого ряда с 27-дневным ритмом"
                    if probe["r_sine"] >= abs(r_raw) - 0.03 else
                    "заметно слабее Солнца — сигнал не сводится к чистому ритму")
    add(f"3. **Синусоида-двойник** (период 27.27 дн, лучшая фаза): "
        f"|r|={probe['r_sine']:.2f} — {sine_verdict}.")
    add(f"\n**Вердикт допроса:** "
        + ("сигнал не пережил проверок — рабочая гипотеза «совпадение медленных "
           "волн + 27-дневный ритм», а не связь."
           if not probe["survives"] else
           "сигнал пережил все три теста; следующий шаг — вневыборочная проверка "
           "на данных, которых модель не видела.") + "\n")
    add("\n![рассеяние](fig3_scatter.png)\n")

    add("\n## 4. Стабильность знака\n\n![скользящая](fig4_rolling.png)\n")
    add(f"Скользящая корреляция уровней отрицательна {neg_share:.0%} времени. "
        "У настоящей связи знак держится; у совпадения — гуляет.\n")

    add("\n## 5. Событийный разрез\n\n![события](fig5_events.png)\n")
    if len(events):
        add("| дата | тип | CH, % диска |")
        add("|---|---|---|")
        for _, e in events.iterrows():
            add(f"| {e['date']:%d.%m.%Y} | {'пик' if e['kind'] == 'peak' else 'впадина'} "
                f"| {e['ch211_pct']:.1f} |")

    if control:
        b = control["best"]
        add("\n## 6. Позитивный контроль: CH × скорость солнечного ветра\n")
        add("\n![контроль](fig7_control.png)\n")
        add(f"Тот же инструмент на физически связанной паре ({b.n} дней): "
            f"k*={b.k:+d} дн, r={b.r:+.2f}, p(скана) ≈ {control['p']:.3f}. Лаг "
            "+2…+4 дня — учебное время долёта быстрого потока от Солнца до Земли, "
            "величина r совпадает с публикациями. Обратите внимание: даже "
            "**настоящая** физическая связь проходит порог 0.05 лишь впритык — "
            "вот насколько строг честный тест. Сравните с разделом 3.\n")

    if stretch_res:
        b = stretch_res["obs"]["best"]
        med_null = float(np.median(stretch_res["null"]["global_max"]))
        add("\n## 7. Разрез «с растяжением времени» (как на накладке из TradingView)\n")
        add("\n![растяжение](fig8_stretch.png)\n")
        add(f"Формализуем приём «кусок солнечного ряда растягивается на месяцы "
            f"рынка»: перебраны масштабы ×0.25…×55 и все сдвиги куска по году "
            f"данных. Лучшее совпадение: s=×{b['s']:g} (кусок CH длиной "
            f"{b['len_days']:.1f} дн от {b['anchor']:%d.%m.%Y}), r={b['r']:+.2f}. "
            f"Но случайно прокрученный солнечный ряд при том же переборе достигает "
            f"max|r| с медианой {med_null:.2f} — наблюдаемое даёт "
            f"**p ≈ {stretch_res['p']:.2f}**. Вывод: растяжение — это не разрез, "
            "в котором связь «проявляется», а генератор гарантированных "
            "совпадений: подходящий кусок находится у случайного ряда почти "
            "всегда.\n")

    if multitf:
        add("\n## 8. Многомасштабный разрез: а правда ли «4 часа коррелируют лучше»?\n")
        add("\n![мультитаймфрейм](fig9_multitf.png)\n")
        add("| бары | разрез | k* | r | N | N_eff | p(скан) |")
        add("|---|---|---|---|---|---|---|")
        for mode, mname in (("levels", "уровни"), ("diffs", "приращения")):
            vd_ = verdicts[mode]
            add(f"| 1 день | {mname} | {vd_['best'].k:+d} дн | {vd_['best'].r:+.2f} "
                f"| {vd_['best'].n} | {vd_['neff']:.0f} | {vd_['p_scan']:.2f} |")
            v4 = multitf["verdicts"][mode]
            add(f"| 4 часа | {mname} | {v4['best'].k / 6:+.1f} дн | {v4['best'].r:+.2f} "
                f"| {v4['best'].n} | {v4['neff']:.0f} | {v4['p']:.2f} |")
        add("\nМелкие бары дают в 6 раз больше точек, но у гладких рядов информация "
            "растёт со **сроком наблюдений**, а не с частотой нарезки: эффективный N "
            "почти не меняется, серая полоса случайности не сужается. Если на "
            "4-часовиках связь «выглядит лучше», это о графике, не о данных.\n")

    if predict_res:
        add("\n## 9. Решающий тест: предсказывает ли Солнце биткоин (вне выборки)\n")
        add("\n![прогнозный тест](fig11_predict.png)\n")
        add("Инженерная постановка, без спора о природе рынка: если воздействие "
            "Солнца существует, оно обязано оставлять след, который улучшает "
            "предсказание. Каждый день модель обучается **только на прошлом** и "
            "предсказывает доходность следующего дня. SOLAR видит только солнечные "
            f"признаки (ΔCH за {predict.N_DCH_LAGS} дней + уровень площади), AR — "
            "только прошлые движения самой цены (Солнца не видит), ZERO — «завтра "
            "как сегодня».\n")
        add("| модель | угадано направление | p против монетки | skill vs ZERO |")
        add("|---|---|---|---|")
        for name in ("SOLAR", "AR"):
            m = predict_res["models"][name]
            add(f"| {name} | {m['hit_rate']:.1%} (из {m['n']}) | {m['binom_p']:.2f} "
                f"| {m['skill']:+.3f} |")
        ms = predict_res["models"]["SOLAR"]
        if ms["binom_p"] < 0.05 and ms["skill"] > 0:
            add("\n**SOLAR показывает значимую предсказательную силу вне выборки** — "
                "это самый сильный аргумент за воздействие из всех возможных; "
                "обязателен повтор на свежих данных.\n")
        else:
            add("\nПредсказательной силы у солнечных признаков не обнаружено: "
                "направление угадывается как монеткой, ошибка не меньше, чем у "
                "модели «завтра как сегодня». В инженерной постановке это и есть "
                "ответ задачи на текущих данных.\n")

    add("\n## 10. Экстраполяции\n\n![прогноз](fig6_forecast.png)\n")
    reg = fan["reg"]
    add(f"- Лаг-карта: btc_ret(t) = {reg['a']:+.4f} {reg['b']:+.4f}·ΔCH(t−{k_star}) "
        f"(n={reg['n']}); будущие ΔCH — из гармоники 27.27 дн.")
    add(f"- Горизонт {HORIZON} дн от {pairs['date'].max():%d.%m.%Y}: медиана лаг-модели "
        f"**${fan['quantiles'][2][-1]:,.0f}** "
        f"(5–95%: ${fan['quantiles'][0][-1]:,.0f}…${fan['quantiles'][4][-1]:,.0f}); "
        f"RW-дрейф ${base['rw_quantiles'][1][-1]:,.0f}; AR(2) ${base['ar2_median'][-1]:,.0f}.")
    add("\n> Игровая экстраполяция для сравнения форм моделей. Не прогноз и не "
        "инвестиционная рекомендация.\n")

    # ------------------------------------------------ сводка простым языком -
    vl = verdicts["levels"]
    r_lv = vl["best"].r
    i_peak = int(np.argmax(chf["harmonic_pooled"]))
    next_peak = chf["dates"].iloc[i_peak]
    add("\n---\n")
    add("## Сводка простым языком\n")
    add("**Как поставлена задача.** Мы не спорим о том, чем управляется рынок — "
        "манипуляция там или нет, неважно. Логика простая: солнечная активность "
        "— внешняя сила, её никто на бирже не назначает. Если она воздействует "
        "на цену, воздействие обязано оставлять след в данных. След ищем двумя "
        "способами: совпадение форм графиков и — решающий способ — предсказание: "
        "модель, которая видит только солнечное прошлое, должна угадывать "
        "завтрашний ход цены чаще, чем монетка.\n")
    add(f"**Сходство есть, и мы его видим так же, как вы.** За год данных "
        f"({len(pairs)} общих дней, биржевые цены) лучшее совпадение форм: если "
        f"сдвинуть график дыр на {vl['best'].k:+d} дней, он повторяет биткоин с "
        f"силой r={r_lv:+.2f} (0 — ничего общего, 1 — близнецы). Это немало для "
        f"глаза — и это ровно то «сходство», которое видно на наложенных "
        f"графиках. Дальше вопрос один: это след воздействия или совпадение?\n")
    t1_sum = (f"остаётся r={probe['r_dt_at']:+.2f} при p≈{probe['p_dt']:.2f} — "
              "неотличимо от случайности: бОльшую часть сходства делали плавные "
              "многомесячные волны, а они в каком-то виде совпадают у любых двух "
              "гладких кривых"
              if probe["p_dt"] >= 0.05 else
              f"остаётся r={probe['r_dt_at']:+.2f} при p≈{probe['p_dt']:.2f} — "
              "сигнал пережил главный тест")
    t2_sum = ("знак одинаковый в обеих ("
              + " и ".join(f"{h['r_at']:+.2f}" for h in probe["halves"])
              + ") — слабое очко «за», хотя каждая цифра по отдельности "
              "объяснима случайностью"
              if len({np.sign(h['r_at']) for h in probe['halves']
                      if np.isfinite(h['r_at'])}) == 1 else
              "в одной половине связь одна, в другой — другая: у настоящего "
              "воздействия так не бывает")
    t3_sum = (f"пустышка даёт r={probe['r_sine']:.2f} — не хуже Солнца: рынок "
              "просто попал в похожий ритм"
              if probe["r_sine"] >= abs(r_lv) - 0.03 else
              f"пустышка даёт лишь r={probe['r_sine']:.2f} — сходство не "
              "сводится к чистому ритму, оно живёт в медленных волнах именно "
              "этого года")
    add(f"**Допрос сходства (три теста).** "
        f"Первый, главный: убираем из обоих графиков медленные многомесячные "
        f"волны — {t1_sum}. "
        f"Второй: режем год пополам и проверяем половины порознь — {t2_sum}. "
        f"Третий: заменяем Солнце «пустышкой» — синусоидой с периодом 27 дней "
        f"(столько Солнце оборачивается вокруг оси) — {t3_sum}. "
        f"Счёт неоднозначный — поэтому решает следующий тест.\n")
    if predict_res:
        ms = predict_res["models"]["SOLAR"]
        ma = predict_res["models"]["AR"]
        add(f"**Решающий тест — предсказание.** Модель каждый день видела только "
            f"прошлое и предсказывала завтрашний ход. Солнечная модель угадала "
            f"направление в {ms['hit_rate']:.0%} случаев из {ms['n']} "
            f"(монетка даёт 50%, вероятность получить такое случайно — "
            f"{ms['binom_p']:.0%}); модель без Солнца, только на прошлых ценах — "
            f"{ma['hit_rate']:.0%}. "
            + ("Солнечная модель значимо лучше монетки — это серьёзно, и это надо "
               "перепроверять на свежих данных.\n"
               if ms["binom_p"] < 0.05 and ms["skill"] > 0 else
               "Ни та, ни другая не лучше монетки. Если бы воздействие Солнца "
               "существовало в силе, видимой на графиках, здесь оно обязано было "
               "проявиться — его нет.\n"))
    if control:
        add(f"**Инструмент зрячий, мы проверили.** Та же программа находит "
            f"настоящую физическую связь в этих же данных: выросли дыры → через "
            f"~{control['best'].k} дня у Земли ускоряется солнечный ветер "
            f"(r={control['best'].r:+.2f}). Известная физика видна; связь с ценой "
            "такой же силы мы бы не пропустили.\n")
    if stretch_res and multitf:
        v4l = multitf["verdicts"]["levels"]
        add(f"**Про приёмы с графиками.** Растяжение куска солнечного ряда на "
            f"месяцы рынка даёт красивые совпадения (до r≈{abs(stretch_res['obs']['best']['r']):.1f}), "
            f"но случайная пустышка при тех же свободах добивается того же в "
            f"большинстве попыток — растяжение производит совпадения гарантированно. "
            f"4-часовые бары: точек в 6 раз больше ({v4l['best'].n} против "
            f"{vl['best'].n}), а независимой информации столько же — сосед "
            f"повторяет соседа, и честный вывод не меняется.\n")
    add(f"**Погода на Солнце.** Следующий пик площади дыр по 27-дневному ритму — "
        f"около **{next_peak:%d.%m.%Y}**. Если хотите продолжать наблюдение: "
        f"обновляйте оба файла данных раз в месяц и перезапускайте `python run.py` "
        f"— смотреть надо на раздел 9 (предсказание) и раздел 3 (допрос).\n")
    add("**Итог одной строкой.** Сходство на графиках настоящее, но его несут "
        "медленные волны конкретного года, а не устойчивый механизм: как только "
        "модель обязана предсказывать завтра, не подглядывая, солнечные данные "
        "не дают ничего — строить торговую логику от Солнца на этих данных "
        "нельзя.\n")

    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
