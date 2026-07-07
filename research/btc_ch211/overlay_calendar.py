#!/usr/bin/env python3
"""«Календарная правда»: реальные числа CH-211 поверх BTC дата-к-дате,
без растяжений, сдвигов и подбора куска — единственно допустимое наложение,
когда мы проверяем связь, а не рисуем её.

Запуск: python overlay_calendar.py [YYYY-MM-DD YYYY-MM-DD]
Выход:  out/fig12_overlay_calendar.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import data_io, plots  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"


def main() -> None:
    lo = pd.Timestamp(sys.argv[1]) if len(sys.argv) > 2 else pd.Timestamp("2026-02-01")
    hi = pd.Timestamp(sys.argv[2]) if len(sys.argv) > 2 else pd.Timestamp("2026-07-07")
    plots.setup_style()

    ch, _ = data_io.load_ch211()
    btc, _ = data_io.load_btc()
    ch = ch[(ch["date"] >= lo) & (ch["date"] <= hi)]
    btc = btc[(btc["date"] >= lo) & (btc["date"] <= hi)]

    def z(s: pd.Series) -> pd.Series:
        return (s - s.mean()) / s.std(ddof=1)

    btc_z = z(np.log(btc["close_usd"]))
    ch_z = pd.Series(z(ch["ch211_pct"]).to_numpy(), index=ch["date"])
    ch_s = ch_z.rolling(7, center=True, min_periods=4).mean()  # как «картинка»: сглажено

    both = pd.DataFrame({"b": pd.Series(btc_z.to_numpy(), index=btc["date"])}).join(
        pd.DataFrame({"c": ch_z, "cs": ch_s}), how="inner").dropna()
    r_raw = float(np.corrcoef(both["b"], both["c"])[0, 1])
    r_smooth = float(np.corrcoef(both["b"], both["cs"])[0, 1])

    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10.6, 5.2))
    fig.subplots_adjust(left=0.07, right=0.87, top=0.86, bottom=0.11)
    ax.plot(btc["date"], btc_z, color=plots.C_BTC, lw=2)
    for _, g in ch.groupby("window"):
        gi = ch_z.loc[g["date"]]
        gs = ch_s.loc[g["date"]]
        ax.plot(g["date"], gi, color=plots.C_CH, lw=1.1, alpha=0.45)
        ax.plot(g["date"], gs, color=plots.C_CH, lw=2.4)
    plots._endlabel(ax, btc["date"].iloc[-1], float(btc_z.iloc[-1]), "BTC (лог, z)",
                    plots.C_BTC)
    last = ch_s.dropna()
    plots._endlabel(ax, last.index[-1], float(last.iloc[-1]),
                    "CH 211Å (сглаж. 7д)", plots.C_CH)
    ax.set_ylabel("z-score за период")
    plots._date_axis(ax)
    ax.set_title(f"Календарь-к-календарю, без растяжений: {lo:%d.%m.%Y} – {hi:%d.%m.%Y}\n"
                 f"r(уровни) = {r_raw:+.2f}; r(CH сглажена, как «картинка») = {r_smooth:+.2f} "
                 f"— вершины НЕ сходятся, когда подгонка запрещена")
    fig.savefig(OUT / "fig12_overlay_calendar.png", bbox_inches="tight")
    print(f"r_raw={r_raw:+.3f}  r_smooth={r_smooth:+.3f}  n={len(both)}")
    print(f"→ {OUT}/fig12_overlay_calendar.png")


if __name__ == "__main__":
    main()
