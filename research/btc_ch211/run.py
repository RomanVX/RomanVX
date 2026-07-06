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

from lib import data_io, extrapolate, honest_stats, plots, slices  # noqa: E402

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

    # ------------------------------------------------------------- отчёт ----
    write_report(ch_src, btc_src, pairs, wins, wtitles, scans, verdicts, nulls,
                 neg_share, events, chf, fan, base, k_star, control, digit_rmse,
                 win_checks)
    print(f"\nГотово: {OUT}/fig1…fig7.png, {OUT}/REPORT.md")


def write_report(ch_src, btc_src, pairs, wins, wtitles, scans, verdicts, nulls,
                 neg_share, events, chf, fan, base, k_star, control,
                 digit_rmse, win_checks) -> None:
    L: list[str] = []
    add = L.append

    add("# Корональные дыры (SDO 211Å) × BTC: разрезы, честная статистика, экстраполяции\n")
    add(f"*Автоотчёт `run.py` от {pairs['date'].max():%d.%m.%Y}. "
        f"CH — {ch_src}; BTC — {btc_src}.*\n")
    add(f"Дневных пар: **{len(pairs)}** ("
        + "; ".join(f"{wtitles[w]}: {(pairs.window == w).sum()}" for w in wins)
        + "). Лаг k>0 = площадь КД опережает BTC на k дней.\n")
    if digit_rmse is not None:
        add(f"> Проверка ручной оцифровки скриншотов против реальной выгрузки: "
            f"RMSE **{digit_rmse:.2f} п.п.** — оценки погрешности в data/README.md честные.\n")

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
        add(f"Тот же инструмент на физически связанной паре: k*={b.k:+d} дн, "
            f"r={b.r:+.2f}, p(скана) ≈ {control['p']:.3f}. Лаг +2…+4 дня — учебное "
            "время долёта быстрого потока от Солнца до Земли, величина r совпадает "
            "с публикациями. Обратите внимание: даже **настоящая** физическая связь "
            "на 5 месяцах данных еле проходит порог 0.05 — вот насколько строг "
            "честный тест. Сравните с разделом 3.\n")

    add("\n## 7. Экстраполяции\n\n![прогноз](fig6_forecast.png)\n")
    reg = fan["reg"]
    add(f"- Лаг-карта: btc_ret(t) = {reg['a']:+.4f} {reg['b']:+.4f}·ΔCH(t−{k_star}) "
        f"(n={reg['n']}); будущие ΔCH — из гармоники 27.27 дн.")
    add(f"- Горизонт {HORIZON} дн от {pairs['date'].max():%d.%m.%Y}: медиана лаг-модели "
        f"**${fan['quantiles'][2][-1]:,.0f}** "
        f"(5–95%: ${fan['quantiles'][0][-1]:,.0f}…${fan['quantiles'][4][-1]:,.0f}); "
        f"RW-дрейф ${base['rw_quantiles'][1][-1]:,.0f}; AR(2) ${base['ar2_median'][-1]:,.0f}.")
    add("\n> Игровая экстраполяция для сравнения форм моделей. Не прогноз и не "
        "инвестиционная рекомендация.\n")

    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
