"""Графики. Палитра и правила — по дизайн-системе (валидирована на CVD/контраст):
одна ось (никаких двойных шкал — уровни сводятся в z-score), тонкие линии,
прямые подписи серий, приглушённая сетка, знак корреляции — дивергентная пара.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, MaxNLocator

# --- палитра (light, surface #fcfcfb; проверена scripts/validate_palette.js) ---
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
C_BTC = "#2a78d6"        # категориальный слот 1 (blue)
C_CH = "#1baf7a"         # слот 2 (aqua) — контраст 2.74:1 ⇒ всегда прямые подписи
C_POS, C_NEG = "#2a78d6", "#e34948"          # дивергентная пара для знака r
WIN_COLORS = ["#eda100", "#4a3aa7", "#e87ba4"]  # слоты 3/5/7 — категории «окно»
SEQ_LIGHT, SEQ_MID = "#cde2fb", "#9ec5f4"    # секвенциальный blue для веера
NULLBAND = "#f0efec"

RU_MONTHS = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


def setup_style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "savefig.dpi": 160,
        "font.family": "DejaVu Sans", "font.size": 10,
        "text.color": INK, "axes.edgecolor": BASELINE,
        "axes.labelcolor": INK2, "axes.titlecolor": INK,
        "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.axisbelow": True,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "legend.fontsize": 9.5,
    })


def _ru_date(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.day} {RU_MONTHS.get(d.month, d.strftime('%m'))}"


def win_title(g: pd.DataFrame) -> str:
    return f"{_ru_date(g['date'].min())} – {_ru_date(g['date'].max())}"


def _date_axis(ax, weekly: bool = True, interval: int = 1) -> None:
    """Адаптивная ось дат: до ~3 мес — недели, дальше — месяцы (с годом)."""
    lo, hi = ax.get_xlim()
    span = hi - lo
    if span > 200:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda x, _: (lambda d: f"{RU_MONTHS[d.month]} {d:%y}")(mdates.num2date(x))))
    else:
        if span > 100 and interval == 1:
            interval = 2
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0, interval=interval))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda x, _: _ru_date(mdates.num2date(x))))
    ax.grid(axis="x", visible=False)


def _endlabel(ax, x, y, text, color, dx=3.0, dy=0.0) -> None:
    ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                color=color, fontsize=9.5, fontweight="bold", va="center")


def _thousands(ax) -> None:
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:.0f}k"))


def _plot_gapped(ax, df, ycol, color, lw=2.0, **kw):
    """Линия с разрывами между окнами."""
    for _, g in df.groupby("window"):
        ax.plot(g["date"], g[ycol], color=color, lw=lw, **kw)


# ------------------------------------------------------------- fig 1 --------

def fig_overview(btc: pd.DataFrame, pairs: pd.DataFrame, ch: pd.DataFrame,
                 path: str) -> None:
    wins = list(pairs["window"].unique())
    fig, axes = plt.subplots(1 + len(wins), 1, figsize=(10.2, 3.5 * (1 + len(wins))))
    fig.subplots_adjust(hspace=0.45, left=0.08, right=0.92, top=0.96, bottom=0.05)

    ax = axes[0]
    ax.plot(btc["date"], btc["close_usd"], color=C_BTC, lw=2)
    for w, g in ch.groupby("window"):
        ax.axvspan(max(g["date"].min(), btc["date"].min() - pd.Timedelta(days=2)),
                   min(g["date"].max(), btc["date"].max() + pd.Timedelta(days=2)),
                   color=C_CH, alpha=0.10, lw=0)
    anchors = btc[(btc["sigma_usd"] > 0) & (btc["sigma_usd"] <= 600)]
    if len(anchors):
        ax.scatter(anchors["date"], anchors["close_usd"], s=16, zorder=3,
                   color=C_BTC, edgecolor=SURFACE, linewidth=1)
        src_note = "точки — датированные якоря из новостей; "
    else:
        src_note = "биржевые данные; "
    _endlabel(ax, btc["date"].iloc[-1], btc["close_usd"].iloc[-1], "BTC/USD", C_BTC)
    _thousands(ax)
    _date_axis(ax, interval=2)
    ax.set_title(f"BTC/USD ({src_note}зелёная заливка — есть данные CH-211Å)")

    for ax, w, cwin in zip(axes[1:], wins, WIN_COLORS):
        g = pairs[pairs["window"] == w]
        ax.plot(g["date"], g["btc_z"], color=C_BTC, lw=2, marker="o", ms=3.6,
                markeredgecolor=SURFACE, markeredgewidth=0.7)
        ax.plot(g["date"], g["ch_z"], color=C_CH, lw=2, marker="o", ms=3.6,
                markeredgecolor=SURFACE, markeredgewidth=0.7)
        _endlabel(ax, g["date"].iloc[-1], g["btc_z"].iloc[-1], "BTC", C_BTC)
        _endlabel(ax, g["date"].iloc[-1], g["ch_z"].iloc[-1], "CH 211Å", C_CH)
        ax.set_ylabel("z-score внутри окна")
        _date_axis(ax)
        i_pk = g["ch211_pct"].idxmax()
        ax.annotate("пик CH", (g.loc[i_pk, "date"], g.loc[i_pk, "ch_z"]),
                    xytext=(0, 9), textcoords="offset points", ha="center",
                    color=INK2, fontsize=8.5)
        ax.set_title(f"Окно {win_title(g)}: обе серии на одной шкале (z-score)")

    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 2 --------

def fig_lagscan(scans: dict, scopes: list[tuple[str, str]], nulls: dict,
                path: str, suptitle: str) -> None:
    """scans[(mode, scope)] -> list[LagResult]; nulls[mode] — полоса для 'pooled'."""
    modes = [("levels", "уровни (z в окне)"), ("diffs", "приращения ΔCH × лог-дох. BTC")]
    ncol = len(scopes)
    fig, axes = plt.subplots(2, ncol, figsize=(4.2 * ncol, 7.0), sharey=True,
                             squeeze=False)
    fig.subplots_adjust(hspace=0.5, wspace=0.12, left=0.075, right=0.985,
                        top=0.88, bottom=0.10)

    for i, (mode, mtitle) in enumerate(modes):
        for j, (scope, stitle) in enumerate(scopes):
            ax = axes[i][j]
            res = scans[(mode, scope)]
            ks = np.array([r.k for r in res])
            rs = np.array([r.r for r in res])
            if scope == "pooled" and mode in nulls:
                q95 = nulls[mode]["per_lag_q95"]
                band = np.array([q95.get(int(k), np.nan) for k in ks])
                ax.fill_between(ks, -band, band, color=NULLBAND, zorder=0)
                ax.annotate("серое: 95% чистой случайности\n(циркулярные суррогаты)",
                            (0.02, 0.03), xycoords="axes fraction", fontsize=7.8,
                            color=MUTED)
            colors = [C_POS if r >= 0 else C_NEG for r in np.nan_to_num(rs)]
            ax.bar(ks, rs, width=0.82, color=colors, zorder=2)
            ax.axhline(0, color=BASELINE, lw=1)
            ax.axvline(0, color=GRID, lw=0.8)
            fin = np.isfinite(rs)
            if fin.any():
                ib = int(np.nanargmax(np.abs(np.where(fin, rs, 0))))
                ax.annotate(f"k*={ks[ib]:+d}, r={rs[ib]:+.2f}",
                            (ks[ib], rs[ib]),
                            xytext=(0, -13 if rs[ib] < 0 else 5),
                            textcoords="offset points", ha="center",
                            fontsize=8.5, color=INK, fontweight="bold")
            ax.set_ylim(-1.05, 1.05)
            ax.set_title(stitle, fontsize=10)
            if i == 1:
                ax.set_xlabel("лаг k, дней (k>0: CH раньше)", fontsize=9)
            if j == 0:
                ax.set_ylabel(f"r\n{mtitle}", fontsize=9.5)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=7))

    fig.suptitle(suptitle, fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 3 --------

def fig_scatter(picks: list[dict], win_titles: dict[str, str], path: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.8))
    fig.subplots_adjust(wspace=0.28, left=0.08, right=0.98, top=0.86, bottom=0.14)
    wins = list(win_titles)

    for ax, p in zip(axes, picks):
        x, y, wl = p["x"], p["y"], p["wins"]
        for w, color in zip(wins, WIN_COLORS):
            m = (wl == w) & np.isfinite(x) & np.isfinite(y)
            if m.any():
                ax.scatter(x[m], y[m], s=30, color=color, alpha=0.85,
                           label=win_titles[w], edgecolor=SURFACE, linewidth=0.7)
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() >= 3:
            b, a = np.polyfit(x[m], y[m], 1)
            xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 50)
            ax.plot(xs, a + b * xs, color=INK2, lw=1.6, ls="--")
        lo, hi = p["ci"]
        ax.set_title(p["title"], fontsize=10.5)
        ax.text(0.03, 0.03,
                f"k*={p['k']:+d} дн · r={p['r']:+.2f} (90% CI {lo:+.2f}…{hi:+.2f})\n"
                f"N={p['n']} · N_eff≈{p['neff']:.0f} · p≈{p['p']:.3f}",
                transform=ax.transAxes, fontsize=8.8, color=INK2, va="bottom")
        ax.set_xlabel(p["xlabel"], fontsize=9.5)
        ax.set_ylabel(p["ylabel"], fontsize=9.5)
        ax.axhline(0, color=GRID, lw=0.8)
        ax.axvline(0, color=GRID, lw=0.8)
        ax.legend(loc="upper right")

    fig.suptitle("Рассеяние на лучших лагах — цвет точки = окно данных",
                 fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 4 --------

def fig_rolling(roll_lv: pd.DataFrame, roll_df: pd.DataFrame, win: int,
                neg_share: float, path: str) -> None:
    fig, ax = plt.subplots(figsize=(10.2, 4.2))
    fig.subplots_adjust(left=0.08, right=0.88, top=0.86, bottom=0.14)
    for df, color, label in ((roll_lv, C_BTC, "уровни"), (roll_df, C_CH, "приращения")):
        ax.plot(df["date"], df["r"], color=color, lw=2)
        last = df.dropna(subset=["r"])
        if len(last):
            _endlabel(ax, last["date"].iloc[-1], last["r"].iloc[-1], label, color)
    ax.axhline(0, color=BASELINE, lw=1)
    ax.set_ylim(-1.05, 1.05)
    ax.set_ylabel(f"r (окно {win} дн)")
    _date_axis(ax, interval=2)
    ax.set_title(f"Скользящая корреляция (лаг 0): знак отрицателен {neg_share:.0%} времени — "
                 "устойчивость знака и есть главный тест «настоящести» связи")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 5 --------

def fig_events(study: dict, path: str) -> None:
    kinds = [("peak", "Пики площади CH → путь BTC"),
             ("trough", "Впадины площади CH → путь BTC")]
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.6), sharey=True)
    fig.subplots_adjust(wspace=0.14, left=0.075, right=0.98, top=0.84, bottom=0.15)
    for ax, (kind, title) in zip(axes, kinds):
        data = study.get(kind)
        ax.axhline(100, color=BASELINE, lw=1)
        ax.axvline(0, color=GRID, lw=0.9)
        n_ev = 0
        if data and data["paths"]:
            days = data["days"]
            mat = []
            for date, seg in data["paths"]:
                ax.plot(days, seg, color=C_BTC, lw=1.1, alpha=0.35)
                fin = np.where(np.isfinite(seg))[0]
                if len(fin):
                    ax.annotate(_ru_date(date), (days[fin[-1]], seg[fin[-1]]),
                                xytext=(3, 0), textcoords="offset points",
                                fontsize=8, color=MUTED, va="center")
                mat.append(seg)
            n_ev = len(mat)
            mean = np.nanmean(np.vstack(mat), axis=0)
            ax.plot(days, mean, color=C_BTC, lw=2.6)
            _endlabel(ax, days[-1], mean[-1], "среднее", C_BTC, dx=4)
        ax.set_title(f"{title} (событий: {n_ev})", fontsize=10.5)
        ax.set_xlabel("дней от события")
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    axes[0].set_ylabel("BTC, 100 = день события")
    fig.suptitle("Событийный разрез: экстремумы площади CH и путь BTC вокруг них",
                 fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 6 --------

def fig_forecast(ch: pd.DataFrame, chf: dict, btc: pd.DataFrame, fan: dict,
                 base: dict, k_star: int, path: str) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.4, 8.8))
    fig.subplots_adjust(hspace=0.4, left=0.08, right=0.86, top=0.95, bottom=0.06)
    fdates = chf["dates"]

    _plot_gapped(ax1, ch, "ch211_pct", C_CH, lw=1.8)
    pred_pooled, rmse = chf["fit_pooled"]
    span = pd.Series(pd.date_range(ch["date"].min(), fdates.iloc[len(fdates) - 1]))
    ax1.plot(span, np.clip(pred_pooled(span), 0.5, None), color=C_CH, lw=1.0,
             alpha=0.5, ls=":")
    styles = [("harmonic_pooled", "-", "гармоника 27.27д (вся история)"),
              ("harmonic_recent", "--", "гармоника (последнее окно)"),
              ("holt", "-.", "Holt демпфированный")]
    for key, ls, label in styles:
        ax1.plot(fdates, chf[key], color=INK2, lw=1.8, ls=ls)
        _endlabel(ax1, fdates.iloc[len(fdates) - 1], chf[key][-1], label, INK2)
    ax1.axvline(ch["date"].max(), color=GRID, lw=1)
    last = ch.iloc[-1]
    _endlabel(ax1, last["date"], last["ch211_pct"], "CH 211Å", C_CH, dx=-42)
    ax1.set_ylabel("площадь CH, % диска")
    _date_axis(ax1, interval=2)
    ax1.set_title("Площадь корональных дыр (SDO 211Å): подгонка и три экстраполяции "
                  f"(RMSE гармоники ±{rmse:.1f} п.п.)")

    hist = btc[btc["date"] >= btc["date"].max() - pd.Timedelta(days=45)]
    ax2.plot(hist["date"], hist["close_usd"], color=C_BTC, lw=2)
    q = fan["quantiles"]
    ax2.fill_between(fdates, q[0], q[4], color=SEQ_LIGHT, lw=0, zorder=0)
    ax2.fill_between(fdates, q[1], q[3], color=SEQ_MID, lw=0, zorder=1)
    ax2.plot(fdates, q[2], color=C_BTC, lw=2)
    _endlabel(ax2, fdates.iloc[len(fdates) - 1], q[2][-1],
              f"лаг-карта ΔCH (k*={k_star:+d})", C_BTC)
    rw = base["rw_quantiles"]
    ax2.plot(fdates, rw[1], color=INK2, lw=1.6, ls="--")
    _endlabel(ax2, fdates.iloc[len(fdates) - 1], rw[1][-1], "RW-дрейф", INK2)
    ax2.plot(fdates, base["ar2_median"], color=INK2, lw=1.6, ls=":")
    _endlabel(ax2, fdates.iloc[len(fdates) - 1], base["ar2_median"][-1], "AR(2)", INK2)
    ax2.axvline(btc["date"].max(), color=GRID, lw=1)
    _thousands(ax2)
    ax2.set_ylabel("BTC/USD")
    _date_axis(ax2)
    ax2.set_title("BTC: веер «солнечной» лаг-модели (полосы 5–95% и 25–75%) и наивные "
                  "бейзлайны — игровая экстраполяция, не прогноз")

    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ fig 13 --------

C_SW = "#4a3aa7"  # слот 5 (violet) — скорость СВ как отдельная сущность


def fig_swtrade(sw: pd.DataFrame, events: pd.DataFrame, table: pd.DataFrame,
                delays, holds, path: str) -> None:
    """Торговый тест: горбы скорости + матрицы (d,h) избыточной доходности."""
    fig = plt.figure(figsize=(11.6, 9.0))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 1], hspace=0.42, wspace=0.22,
                          left=0.07, right=0.97, top=0.93, bottom=0.07)
    ax = fig.add_subplot(gs[0, :])
    d = sw.dropna(subset=["sw211_kms"])
    ax.plot(d["date"], d["sw211_kms"], color=C_SW, lw=1.8)
    _endlabel(ax, d["date"].iloc[-1], d["sw211_kms"].iloc[-1], "SW 211Å", C_SW)
    for kind, marker, color, label in (("trough", "^", C_CH, "подтверждено дно"),
                                       ("peak", "v", C_NEG, "подтверждён верх")):
        e = events[events["kind"] == kind]
        vals = [float(d.loc[d["date"] == t, "sw211_kms"].iloc[0])
                if (d["date"] == t).any() else np.nan for t in e["confirmed"]]
        ax.scatter(e["confirmed"], vals, marker=marker, s=64, color=color,
                   zorder=3, edgecolor=SURFACE, linewidth=1, label=label)
    ax.legend(loc="upper right", ncols=2)
    ax.set_ylabel("модельная скорость СВ, км/с")
    _date_axis(ax)
    ax.set_title("Скорость солнечного ветра (модель по SDO 211Å): горбы и причинные "
                 "точки входа (подтверждение = 2 движения от экстремума)")

    for j, (kind, ttl) in enumerate((("trough", "LONG со дна"),
                                     ("peak", "SHORT с верха"))):
        axh = fig.add_subplot(gs[1, j])
        mat = np.full((len(delays), len(holds)), np.nan)
        pm = np.full_like(mat, np.nan)
        for _, r in table[table["kind"] == kind].iterrows():
            i, k = delays.index(int(r["delay"])), holds.index(int(r["hold"]))
            mat[i, k] = r["excess_pct"]
            pm[i, k] = r["p_rand"]
        vmax = np.nanmax(np.abs(mat)) or 1.0
        im = axh.imshow(mat, cmap=_diverging_cmap(), vmin=-vmax, vmax=vmax,
                        aspect="auto")
        for i in range(len(delays)):
            for k in range(len(holds)):
                if np.isfinite(mat[i, k]):
                    star = "*" if pm[i, k] < 0.05 else ""
                    axh.text(k, i, f"{mat[i, k]:+.1f}{star}", ha="center",
                             va="center", fontsize=8.6, color=INK)
        axh.set_xticks(range(len(holds)), [f"{h}д" for h in holds])
        axh.set_yticks(range(len(delays)), [f"{dd:+d}д" for dd in delays])
        axh.set_xlabel("удержание")
        if j == 0:
            axh.set_ylabel("вход относительно экстремума")
        axh.grid(visible=False)
        axh.set_title(f"{ttl} (избыток, п.п.)", fontsize=10)
    fig.suptitle("Торговый тест от горбов скорости СВ — избыток каждой клетки "
                 "к таким же входам в случайные даты (* = p<0.05 до поправки)",
                 fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _diverging_cmap():
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(
        "div", ["#e34948", "#f0efec", "#2a78d6"])


# ------------------------------------------------------------ fig 11 --------

def fig_predict(pr: dict, path: str) -> None:
    """Walk-forward: кумулятивные кривые знаковых стратегий + скользящий hit-rate."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.4, 8.0))
    fig.subplots_adjust(hspace=0.42, left=0.08, right=0.86, top=0.89, bottom=0.07)
    dates = pr["dates"]

    curves = [("SOLAR", C_CH, "-", "по Солнцу"),
              ("AR", INK2, "--", "по прошлым ценам")]
    for name, color, ls, label in curves:
        c = pr["models"][name]["strategy_curve"] * 100
        ax1.plot(dates, c, color=color, lw=2, ls=ls)
        _endlabel(ax1, dates.iloc[-1], c[-1], label, color)
    bh = pr["buy_hold"] * 100
    ax1.plot(dates, bh, color=C_BTC, lw=1.6, ls=":")
    _endlabel(ax1, dates.iloc[-1], bh[-1], "просто держать", C_BTC)
    ax1.axhline(0, color=BASELINE, lw=1)
    ax1.set_ylabel("накопленный результат, %")
    _date_axis(ax1)
    ax1.set_title("Игрушечные знаковые стратегии (без комиссий): предсказал рост — "
                  "«купил», падение — «продал»")

    win = 60
    for (name, color, ls, label), dy in zip(curves, (-8, 8)):
        p = pr["models"][name]["pred"]
        hit = pd.Series((np.sign(p) == np.sign(pr["actual"])).astype(float))
        roll = hit.rolling(win, min_periods=win).mean()
        ax2.plot(dates, roll, color=color, lw=2, ls=ls)
        last = roll.dropna()
        if len(last):
            _endlabel(ax2, dates.iloc[last.index[-1]], last.iloc[-1], label,
                      color, dy=dy)
    ax2.axhline(0.5, color=BASELINE, lw=1.2)
    ax2.annotate("монетка = 0.50", (dates.iloc[2], 0.5), xytext=(0, 5),
                 textcoords="offset points", color=MUTED, fontsize=8.5)
    ax2.set_ylim(0.25, 0.75)
    ax2.set_ylabel(f"доля угаданных направлений (окно {win} дн)")
    _date_axis(ax2)
    ms = pr["models"]["SOLAR"]
    ax2.set_title(f"Скользящая точность направления: SOLAR = {ms['hit_rate']:.1%} "
                  f"всего (p против монетки ≈ {ms['binom_p']:.2f})")

    fig.suptitle("Решающий тест: каждый день модель видит только прошлое и "
                 "предсказывает завтра", fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ fig 10 --------

def fig_probe(probe: dict, r_raw: float, path: str) -> None:
    """Допрос пограничного сигнала: детренд-скан + сравнение с двойниками."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.6, 4.8))
    fig.subplots_adjust(wspace=0.45, left=0.07, right=0.97, top=0.84, bottom=0.14)

    res = probe["scan_dt"]
    ks = np.array([r.k for r in res])
    rs = np.array([r.r for r in res])
    q95 = probe["null_dt"]["per_lag_q95"]
    band = np.array([q95.get(int(k), np.nan) for k in ks])
    ax1.fill_between(ks, -band, band, color=NULLBAND, zorder=0)
    colors = [C_POS if r >= 0 else C_NEG for r in np.nan_to_num(rs)]
    ax1.bar(ks, rs, width=0.82, color=colors, zorder=2)
    ax1.axhline(0, color=BASELINE, lw=1)
    ax1.annotate(f"на лаге сырого сигнала (k={probe['k_lv']:+d}): "
                 f"r={probe['r_dt_at']:+.2f}\nлучший: r={probe['best_dt'].r:+.2f}, "
                 f"p≈{probe['p_dt']:.2f}",
                 (0.02, 0.96), xycoords="axes fraction", va="top",
                 fontsize=8.8, color=INK, fontweight="bold")
    ax1.set_ylim(-1.05, 1.05)
    ax1.set_xlabel("лаг k, дней (k>0: CH раньше)")
    ax1.set_ylabel("r")
    ax1.set_title(f"Тест 1: детренд (минус MA {probe['trend_win']} дн)",
                  fontsize=10)

    rows = [("CH, сырые уровни (лучший лаг)", abs(r_raw), C_CH),
            ("CH после детренда (тот же лаг)", abs(probe["r_dt_at"]), C_CH),
            ("синусоида 27.3 дн (лучшая фаза)", probe["r_sine"], "#eda100")]
    null_med = float(np.median(probe["null_dt"]["max_abs"]))
    null_q95 = float(np.quantile(probe["null_dt"]["max_abs"], 0.95))
    rows += [("случайная прокрутка: медиана", null_med, BASELINE),
             ("случайная прокрутка: топ-5%", null_q95, BASELINE)]
    ypos = np.arange(len(rows))[::-1]
    for y, (label, v, c) in zip(ypos, rows):
        ax2.barh(y, v, height=0.62, color=c)
        ax2.annotate(f"{v:.2f}", (v, y), xytext=(4, 0), textcoords="offset points",
                     va="center", fontsize=9, color=INK2)
    ax2.set_yticks(ypos, [r[0] for r in rows], fontsize=9)
    ax2.set_xlim(0, 1.0)
    ax2.set_xlabel("|r|")
    ax2.grid(axis="y", visible=False)
    h_txt = " и ".join(f"{h['r_at']:+.2f}" for h in probe["halves"])
    ax2.set_title(f"Тест 3: двойники · Тест 2 (половины года): r = {h_txt}",
                  fontsize=10)

    fig.suptitle("Допрос пограничного сигнала в уровнях", fontsize=11.5,
                 fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 9 --------

def fig_multitf(scans4: dict, nulls4: dict, verdicts4: dict, bar_hours: float,
                path: str) -> None:
    """Лаг-скан на мелких барах (напр. 4ч): x в днях, полоса нуля своя."""
    modes = [("levels", "уровни (z в окне)"), ("diffs", "приращения")]
    fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.6), sharey=True)
    fig.subplots_adjust(wspace=0.1, left=0.07, right=0.98, top=0.82, bottom=0.15)
    step = bar_hours / 24.0
    for ax, (mode, mtitle) in zip(axes, modes):
        res = scans4[mode]
        kd = np.array([r.k for r in res]) * step
        rs = np.array([r.r for r in res])
        q95 = nulls4[mode]["per_lag_q95"]
        band = np.array([q95.get(int(r.k), np.nan) for r in res])
        ax.fill_between(kd, -band, band, color=NULLBAND, zorder=0)
        colors = [C_POS if r >= 0 else C_NEG for r in np.nan_to_num(rs)]
        ax.bar(kd, rs, width=step * 0.85, color=colors, zorder=2)
        ax.axhline(0, color=BASELINE, lw=1)
        ax.axvline(0, color=GRID, lw=0.8)
        v = verdicts4[mode]
        b = v["best"]
        ax.annotate(f"k*={b.k * step:+.1f} дн, r={b.r:+.2f}\n"
                    f"N={b.n}, но N_eff≈{v['neff']:.0f}\np(скана)≈{v['p']:.2f}",
                    (0.02, 0.96), xycoords="axes fraction", va="top",
                    fontsize=8.8, color=INK, fontweight="bold")
        ax.set_ylim(-1.05, 1.05)
        ax.set_xlabel("лаг, дней (k>0: CH раньше)")
        ax.set_title(mtitle, fontsize=10.5)
    axes[0].set_ylabel("r")
    fig.suptitle(f"Бары по {bar_hours:g} ч: точек в {24 / bar_hours:g} раза больше, "
                 "информации — нет (серая полоса нуля не сузилась)",
                 fontsize=11.5, fontweight="bold")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 8 --------

def fig_stretch(btc: pd.DataFrame, obs: dict, null: dict, global_p: float,
                scales: list[float], path: str) -> None:
    """Разрез с растяжением времени: лучшее наложение + скан по масштабам."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.4, 8.2))
    fig.subplots_adjust(hspace=0.42, left=0.08, right=0.9, top=0.93, bottom=0.08)

    best = obs["best"]
    y = np.log(btc["close_usd"].to_numpy())
    yz = (y - y.mean()) / y.std(ddof=1)
    sv = best["stretched"]
    svz = (sv - sv.mean()) / sv.std(ddof=1)
    ax1.plot(btc["date"], yz, color=C_BTC, lw=2)
    ax1.plot(btc["date"], svz, color=C_CH, lw=2)
    _endlabel(ax1, btc["date"].iloc[-1], yz[-1], "BTC (лог, z)", C_BTC)
    _endlabel(ax1, btc["date"].iloc[-1], svz[-1], "CH растянутый", C_CH)
    ax1.set_ylabel("z-score")
    _date_axis(ax1)
    a0, a1 = best["anchor"], best["anchor"] + pd.Timedelta(days=best["len_days"])
    ax1.set_title(f"Лучшее из всех растяжений: s=×{best['s']:g} — кусок CH "
                  f"{_ru_date(a0)}–{_ru_date(a1)} растянут на весь период BTC; "
                  f"r={best['r']:+.2f}")

    xs = np.arange(len(scales))
    obs_r = [abs(p["r"]) if np.isfinite(p["r"]) else np.nan
             for p in obs["per_scale"]]
    band = [null["per_scale_q95"].get(s, np.nan) for s in scales]
    ax2.fill_between(xs, 0, band, color=NULLBAND, zorder=0)
    ax2.plot(xs, obs_r, color=C_BTC, lw=2, marker="o", ms=6,
             markeredgecolor=SURFACE, markeredgewidth=0.9)
    ax2.annotate("серое: 95% лучших совпадений, которых добивается\n"
                 "СЛУЧАЙНО прокрученный солнечный ряд с теми же свободами",
                 (0.02, 0.05), xycoords="axes fraction", fontsize=8.4, color=MUTED)
    i55 = int(np.argmin(np.abs(np.array(scales) - 55)))
    ax2.annotate("≈ масштаб вашей накладки", (xs[i55], obs_r[i55]),
                 xytext=(0, 10), textcoords="offset points", ha="right",
                 fontsize=8.6, color=INK2)
    ax2.set_xticks(xs, [f"×{s:g}" for s in scales])
    ax2.set_xlabel("растяжение s (1 день солнечного ряда = s дней рынка)")
    ax2.set_ylabel("лучший |r| по всем сдвигам")
    ax2.set_ylim(0, 1.05)
    ax2.set_title(f"Скан масштабов: наблюдаемое против случайности — "
                  f"p(всего перебора) ≈ {global_p:.2f}")

    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- fig 7 --------

def fig_control(sw_pairs: pd.DataFrame, scan, null: dict, best, path: str) -> None:
    """Позитивный контроль: CH-211 × фактическая скорость СВ."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.4, 7.6))
    fig.subplots_adjust(hspace=0.45, left=0.08, right=0.9, top=0.94, bottom=0.08)

    _plot_gapped(ax1, sw_pairs, "ch_z", C_CH, lw=1.8)
    _plot_gapped(ax1, sw_pairs, "btc_z", C_BTC, lw=1.8)
    lastrow = sw_pairs.iloc[-1]
    _endlabel(ax1, lastrow["date"], lastrow["ch_z"], "CH 211Å", C_CH)
    _endlabel(ax1, lastrow["date"], lastrow["btc_z"], "скорость СВ", C_BTC)
    ax1.set_ylabel("z-score внутри окна")
    _date_axis(ax1, interval=2)
    ax1.set_title("Позитивный контроль: площадь CH и фактическая скорость солнечного ветра "
                  "(Bulk speed) — физически связанные ряды")

    ks = np.array([r.k for r in scan])
    rs = np.array([r.r for r in scan])
    q95 = null["per_lag_q95"]
    band = np.array([q95.get(int(k), np.nan) for k in ks])
    ax2.fill_between(ks, -band, band, color=NULLBAND, zorder=0)
    colors = [C_POS if r >= 0 else C_NEG for r in np.nan_to_num(rs)]
    ax2.bar(ks, rs, width=0.82, color=colors, zorder=2)
    ax2.axhline(0, color=BASELINE, lw=1)
    ax2.axvline(0, color=GRID, lw=0.8)
    ax2.annotate(f"k*={best.k:+d} дн, r={best.r:+.2f}\nвыходит из серой полосы —\n"
                 "инструмент ловит настоящую связь",
                 (0.03, 0.95), xycoords="axes fraction", va="top",
                 fontsize=9, color=INK, fontweight="bold")
    ax2.set_ylim(-1.05, 1.05)
    ax2.set_xlabel("лаг k, дней (k>0: CH раньше)")
    ax2.set_ylabel("r (уровни, z)")
    ax2.set_title("Лаг-скан CH(t) × скорость СВ(t+k): пик на k≈+2…+4 — время долёта "
                  "быстрого потока от Солнца до Земли")
    ax2.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=9))

    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
