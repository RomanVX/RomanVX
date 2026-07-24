"""Снимок кабинета: компактная сводка всего, что происходит.

Вшивается в КАЖДУЮ сессию агента, чтобы он не гадал вслепую, какой
инструмент дёрнуть, а сразу видел общую картину и знал, куда копать.
Собирается из дешёвых источников (БД и уже прогретые кеши), раз в 30 минут
в фоне, хранится в kv — переживает рестарты и не грузит 512 МБ.
"""
import asyncio
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("agent_digest")
_KV = "agent_digest"
_TTL_MIN = 30


def _msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def _rub(v) -> str:
    try:
        return f"{round(float(v)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _sales_block() -> list[str]:
    """Продажи по площадкам: 7 дней против предыдущих 7 (sales_daily, БД)."""
    import db
    d7 = (_msk() - timedelta(days=7)).strftime("%Y-%m-%d")
    d14 = (_msk() - timedelta(days=14)).strftime("%Y-%m-%d")
    d30 = (_msk() - timedelta(days=30)).strftime("%Y-%m-%d")
    out = []
    try:
        cur = {r[0]: (float(r[1] or 0), int(r[2] or 0)) for r in db.fetchall(
            "SELECT platform, SUM(revenue), SUM(qty) FROM sales_daily "
            "WHERE sale_date >= ? GROUP BY platform", (d7,))}
        prev = {r[0]: float(r[1] or 0) for r in db.fetchall(
            "SELECT platform, SUM(revenue) FROM sales_daily "
            "WHERE sale_date >= ? AND sale_date < ? GROUP BY platform", (d14, d7))}
        mon = {r[0]: float(r[1] or 0) for r in db.fetchall(
            "SELECT platform, SUM(revenue) FROM sales_daily "
            "WHERE sale_date >= ? GROUP BY platform", (d30,))}
    except Exception as e:
        return [f"продажи: нет данных ({str(e)[:60]})"]
    parts = []
    for pf in ("WB", "OZON", "YM"):
        rev, qty = cur.get(pf, (0, 0))
        if not rev and not mon.get(pf):
            continue
        was = prev.get(pf, 0)
        delta = f"{(rev - was) / was * 100:+.0f}%" if was else "—"
        parts.append(f"{pf}: 7дн {_rub(rev)} ₽ / {qty} шт ({delta} к пред. неделе), "
                     f"30дн {_rub(mon.get(pf, 0))} ₽")
    if parts:
        out.append("ПРОДАЖИ — " + "; ".join(parts))
    return out


async def _unit_block() -> list[str]:
    """Кто зарабатывает и кто ест деньги — по живым ценам."""
    try:
        from routers import tools as _tools
        import agent_review as _ar
        import wb_client as _wb
        data = await asyncio.wait_for(_tools.get_margin(mp="WB"), timeout=45)
        items = data.get("items") or []
        if not items:
            return ["юнитка ещё собирается"]
        live = {}
        try:
            live = await asyncio.wait_for(_wb.get_current_prices(), timeout=25)
        except Exception:
            pass
        rows = []
        for b in items:
            lp = (live.get(str(b.get("sku"))) or {}).get("discounted")
            m = _ar._margin_math({**b, "price0": lp} if lp else b)
            rows.append(m)
        rows.sort(key=lambda x: x["profit_month"])
        losers = [r for r in rows if r["profit_month"] < 0][:4]
        winners = sorted(rows, key=lambda x: -x["profit_month"])[:4]
        out = []
        if losers:
            out.append("В МИНУС (по живым ценам): " + "; ".join(
                f"{r['sku']} {_rub(r['profit_month'])} ₽/мес "
                f"({r['margin_pct']}% маржа, ДРР {r['drr_pct']}% при "
                f"безубытке {r['be_drr_pct']}%)" for r in losers))
        if winners:
            out.append("ЗАРАБАТЫВАЮТ: " + "; ".join(
                f"{r['sku']} {_rub(r['profit_month'])} ₽/мес ({r['margin_pct']}%)"
                for r in winners))
        return out
    except Exception as e:
        return [f"юнитка недоступна ({str(e)[:60]})"]


async def _stocks_block() -> list[str]:
    try:
        from routers import dashboard as _dash
        rows = await asyncio.wait_for(_dash.get_stocks_table(), timeout=40)
    except Exception as e:
        return [f"остатки недоступны ({str(e)[:60]})"]
    hot = []
    for r in rows or []:
        for pf, dk, qk in (("WB", "wb_days", "wb_qty"),
                           ("Ozon", "oz_days", "oz_qty"),
                           ("ЯМ", "ym_days", "ym_qty")):
            days = r.get(dk)
            if days is not None and days <= 20 and (r.get(qk) or 0) >= 0:
                hot.append(f"{r.get('sku')} {pf} {int(days)}дн")
    if hot:
        return [f"ГОРЯТ ОСТАТКИ ({len(hot)}): " + ", ".join(hot[:12])]
    return ["остатки: горящих нет"]


async def _ads_block() -> list[str]:
    out = []
    try:
        from routers import tools as _tools
        adv = await asyncio.wait_for(_tools.get_adv(), timeout=40)
        out.append(f"РЕКЛАМА WB: расход {_rub(adv.get('total_spend'))} ₽, "
                   f"ДРР {adv.get('total_drr')}%, слив "
                   f"{_rub(adv.get('waste'))} ₽ за {adv.get('days')} дн")
    except Exception:
        pass
    try:
        from routers import tools as _tools
        oz = await asyncio.wait_for(_tools.get_ozads(refresh=False, days=28),
                                    timeout=40)
        out.append(f"РЕКЛАМА OZON: расход {_rub(oz.get('total_spend'))} ₽, "
                   f"ДРР {oz.get('total_drr')}%")
    except Exception:
        pass
    return out


def _reviews_block() -> list[str]:
    try:
        import reviews_client as rc
        table = rc.get_rating_table()
        arts = table.get("articles") or []
        low = []
        for a in arts:
            for pf in ("wb", "ozon", "ym"):
                v, n = a.get(pf), a.get(pf + "_cnt") or 0
                if v and n >= 5 and v < 4.6:
                    low.append(f"{a['sku']} {pf.upper()} {v}")
        unans = len([r for r in rc.get_all_reviews(None, 200)
                     if not r.get("answer") and (r.get("text") or "")])
        s = f"ОТЗЫВЫ: без ответа {unans}"
        if low:
            s += "; рейтинг ниже 4.6: " + ", ".join(low[:6])
        return [s]
    except Exception:
        return []


def _agent_block() -> list[str]:
    out = []
    try:
        import agent_strategist as st
        due = st.due_tasks()
        opened = st._tasks_load("open")
        notes = [t for t in opened if t.get("kind") == "note"]
        out.append(f"ПАМЯТЬ: открытых задач {len(opened)}, "
                   f"на проверку сегодня {len(due)}, фактов от владельца {len(notes)}")
        if due:
            out.append("ПРОВЕРИТЬ СЕГОДНЯ: " + "; ".join(
                f"{t['title'][:70]} (метрика: {(t.get('metric') or '—')[:40]})"
                for t in due[:5]))
    except Exception:
        pass
    try:
        import agent_actions as aa
        pend = aa.pending()
        if pend:
            out.append(f"ЖДУТ ПОДТВЕРЖДЕНИЯ ({len(pend)}): " +
                       "; ".join(p["title"][:60] for p in pend[:5]))
    except Exception:
        pass
    return out


async def build() -> str:
    """Собрать снимок. Тяжёлые куски параллельно и с таймаутами."""
    head = [f"СНИМОК КАБИНЕТА на {_msk().strftime('%Y-%m-%d %H:%M')} МСК"]
    sync = []
    try:
        sync += _sales_block()
    except Exception as e:
        _log.warning("sales: %s", e)
    unit, stocks, ads = await asyncio.gather(
        _unit_block(), _stocks_block(), _ads_block(), return_exceptions=True)
    parts = head + sync
    for chunk in (unit, stocks, ads):
        if isinstance(chunk, list):
            parts += chunk
    try:
        parts += await asyncio.to_thread(_reviews_block)
    except Exception:
        pass
    try:
        parts += await asyncio.to_thread(_agent_block)
    except Exception:
        pass
    return "\n".join(p for p in parts if p)[:6000]


async def get(max_age_min: int = _TTL_MIN) -> str:
    """Снимок из kv; если протух — вернуть как есть и обновить в фоне."""
    import snapshot as _snap
    v = await asyncio.to_thread(_snap.load, _KV, None) or {}
    text, built = v.get("text") or "", v.get("built") or ""
    stale = True
    if built:
        try:
            stale = (_msk() - datetime.strptime(built, "%Y-%m-%d %H:%M")
                     ) > timedelta(minutes=max_age_min)
        except ValueError:
            stale = True
    if stale:
        if text:      # отдаём прошлый, свежий соберём фоном
            asyncio.get_event_loop().create_task(refresh())
        else:
            return await refresh()
    return text


async def refresh() -> str:
    import snapshot as _snap
    try:
        text = await build()
    except Exception as e:
        _log.warning("build: %s", e)
        return ""
    await asyncio.to_thread(_snap.save, _KV,
                            {"text": text,
                             "built": _msk().strftime("%Y-%m-%d %H:%M")})
    return text
