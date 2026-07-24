"""Сторожа агента: он сам замечает проблемы и пишет первым.

Раз в час проходит по проверкам, каждая возвращает список тревог. Дальше
антиспам: одна и та же тревога не повторяется, пока не пропадёт и не появится
снова (дедуп по ключу в kv), тихие часы 23:00–08:00 МСК копятся до утра.
Тревоги отправляются в основной чат одним сообщением.
"""
import asyncio
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("agent_watch")

_KV = "agent_watch_state"          # {ключ: дата последнего алерта}
_PENDING = "agent_watch_pending"   # накопленное за тихие часы
_QUIET = range(23, 24)             # 23:00–08:00 МСК не будим


def _msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def _quiet_now() -> bool:
    h = _msk().hour
    return h >= 23 or h < 8


# ── проверки: каждая возвращает [(ключ, текст)] ──────────────────────────────
async def _w_adv_balance() -> list[tuple]:
    """Баланс рекламы WB: реклама встанет — карточка теряет позиции на дни."""
    try:
        import httpx
        import advert_client as ac
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://advert-api.wildberries.ru/adv/v1/balance",
                            headers=ac._headers())
        bal = r.json() if r.is_success else None
    except Exception:
        return []
    if not isinstance(bal, dict):
        return []
    total = (bal.get("balance") or 0) + (bal.get("bonus") or 0)
    if total and total < 5000:
        return [("adv_balance", f"Баланс рекламы WB {int(total)} ₽ — "
                                "скоро кампании встанут, пополни.")]
    return []


async def _w_hidden_products() -> list[tuple]:
    """Товар в наличии, но продажи в ноль — частый признак скрытия карточки."""
    try:
        import db
        rows = await asyncio.to_thread(
            db.fetchall,
            "SELECT sku, SUM(qty) FROM sales_daily "
            "WHERE sale_date >= ? AND platform = 'WB' GROUP BY sku",
            ((_msk() - timedelta(days=3)).strftime("%Y-%m-%d"),))
        recent = {r[0]: int(r[1] or 0) for r in rows}
        rows_prev = await asyncio.to_thread(
            db.fetchall,
            "SELECT sku, SUM(qty) FROM sales_daily "
            "WHERE sale_date >= ? AND sale_date < ? AND platform = 'WB' GROUP BY sku",
            ((_msk() - timedelta(days=17)).strftime("%Y-%m-%d"),
             (_msk() - timedelta(days=3)).strftime("%Y-%m-%d")))
        prev = {r[0]: int(r[1] or 0) / 14 for r in rows_prev}
    except Exception as e:
        _log.warning("hidden: %s", e)
        return []
    stocks: dict = {}
    try:
        import wb_client
        for row in await wb_client.get_stocks():
            art = str(row.get("supplierArticle") or row.get("sku") or "")
            if art:
                stocks[art] = stocks.get(art, 0) + int(row.get("quantity") or 0)
    except Exception:
        stocks = {}
    out = []
    for sku, was in prev.items():
        now = recent.get(sku, 0) / 3
        if was >= 2 and now == 0 and (stocks.get(sku) or 0) > 5:
            out.append((f"dead_{sku}",
                        f"{sku}: продажи WB упали в ноль за 3 дня, хотя "
                        f"остаток {stocks.get(sku)} шт (было ~{was:.0f} шт/день). "
                        "Проверь, не скрыта ли карточка."))
    return out[:5]


async def _w_docs_expiry() -> list[tuple]:
    """Истекающая декларация = снятие карточки с продажи без предупреждения."""
    try:
        from routers import docs as _docs
        d = await _docs.get_docs_summary(refresh=False)
    except Exception:
        return []
    out = []
    today = _msk().date()
    rows = (d.get("certs") or []) + (d.get("manual") or []) + (d.get("rows") or [])
    for row in rows:
        till = str(row.get("valid_to") or row.get("valid_till") or "")[:10]
        if not till:
            continue
        try:
            left = (datetime.strptime(till, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if 0 < left <= 45:
            out.append((f"doc_{row.get('number') or row.get('id') or till}",
                        f"Документ {row.get('name') or row.get('number') or ''} "
                        f"истекает через {left} дн ({till})."))
    return out[:5]


async def _w_ozon_auto_action() -> list[tuple]:
    """Автоакции Ozon незаметно режут маржу — ловим включённые на убыточных."""
    try:
        import ozon_client
        prices = await ozon_client.get_prices()
    except Exception:
        return []
    on = [a for a, p in (prices or {}).items() if p.get("auto_action_enabled")]
    if len(on) >= 1:
        return [("oz_auto", "Ozon: автодобавление в акции включено у "
                            f"{len(on)} SKU ({', '.join(sorted(on)[:6])}"
                            f"{' и др.' if len(on) > 6 else ''}). "
                            "Площадка может срезать цену без спроса — проверь маржу.")]
    return []


async def _w_stock_out() -> list[tuple]:
    """Маржинальный SKU уходит в ноль — потерянная прибыль и просадка позиций."""
    try:
        import agent_review as _ar
        txt = await _ar.build_stocks_summary()
    except Exception:
        return []
    hot = [ln.strip() for ln in txt.splitlines()
           if "дн" in ln and any(x in ln for x in ("0 дн", "1 дн", "2 дн", "3 дн"))]
    if hot:
        return [("stockout", "Заканчивается товар:\n" + "\n".join(hot[:6]))]
    return []


_CHECKS = (_w_adv_balance, _w_hidden_products, _w_docs_expiry,
           _w_ozon_auto_action, _w_stock_out)


async def tick() -> dict:
    """Один проход сторожей: собрать тревоги, отсеять повторы, отправить."""
    import snapshot as _snap
    import agent_review as _ar
    state = await asyncio.to_thread(_snap.load, _KV, None) or {}
    pending = await asyncio.to_thread(_snap.load, _PENDING, None) or []

    alerts = []
    for check in _CHECKS:
        try:
            alerts += await check()
        except Exception as e:
            _log.warning("%s: %s", check.__name__, str(e)[:200])

    today = _msk().strftime("%Y-%m-%d")
    fresh, seen = [], set()
    for key, text in alerts:
        seen.add(key)
        if state.get(key) == today:      # уже писали сегодня про это
            continue
        state[key] = today
        fresh.append(text)
    # тревога пропала — забываем, чтобы при возврате снова написать
    for key in list(state):
        if key not in seen:
            state.pop(key, None)

    pending += fresh
    sent = 0
    if pending and not _quiet_now():
        head = ("Заметил сам:" if len(pending) == 1
                else f"Заметил сам ({len(pending)}):")
        await _ar.tg_send(head + "\n\n" + "\n\n".join(pending[:10]))
        sent = len(pending)
        pending = []

    await asyncio.to_thread(_snap.save, _KV, state)
    await asyncio.to_thread(_snap.save, _PENDING, pending[:20])
    return {"checked": len(_CHECKS), "fresh": len(fresh), "sent": sent}
