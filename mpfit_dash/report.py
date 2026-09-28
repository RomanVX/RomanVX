"""Расчёты дашборда по своей базе. Дни и часы — по Москве (UTC+3)."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "backend"))  # backend/db.py
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, date

import db
import store

MSK = timedelta(hours=3)
FF_TYPE = "FF"


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _msk(s):
    d = _dt(s)
    return d + MSK if d else None


def _now_msk():
    return datetime.utcnow() + MSK


def _pct(a, b):
    return round((a - b) / b * 100) if b else None


def _q(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(len(v) * p))], 1)


def companies() -> dict:
    return {r[0]: {"name": r[1], "type": r[2]} for r in
            db.fetchall("SELECT id, name, type FROM m_companies")}


def _short(name: str) -> str:
    n = (name or "").replace("Индивидуальный предприниматель", "ИП")
    n = n.replace("ООО ", "").replace("«", "").replace("»", "")
    parts = n.split()
    if parts and parts[0] == "ИП" and len(parts) >= 4:
        return f"{parts[1]} {parts[2][0]}.{parts[3][0]}."
    return n.strip()


def orders() -> list[dict]:
    keys = ("id", "number", "company_id", "source", "status", "created_at", "plan_at",
            "shipped_at", "units", "svc_checked", "svc_revenue", "svc_cost", "svc_missing")
    rows = db.fetchall(f"SELECT {', '.join(keys)} FROM m_orders")
    out = []
    for r in rows:
        o = dict(zip(keys, r))
        o["c"] = _msk(o["created_at"])
        o["s"] = _msk(o["shipped_at"])
        o["p"] = _msk(o["plan_at"])
        # плановая дата валидна, только если это «создан + 24 ч» (у части заказов plan = created)
        o["plan_ok"] = bool(o["p"] and o["c"] and abs((o["p"] - o["c"]).total_seconds() / 3600 - 24) < 1.5)
        out.append(o)
    return out


def _invoice_fbs_price() -> dict:
    """Последняя цена «Отгрузка FBS» за единицу по клиенту из счетов МПФИТ."""
    out = {}
    for cid, ops in db.fetchall("SELECT company_id, ops FROM m_invoices ORDER BY date"):
        for op in json.loads(ops or "[]"):
            if "отгрузка" in (op.get("name") or "").lower() and op.get("price"):
                out[cid] = op["price"]
    return out


def _unit_price(os_: list[dict]) -> dict:
    """Цена «Отгрузки FBS» за единицу по клиенту: медиана по 60 последним
    проверенным заказам (свежая цена, после ДС она меняется), иначе из счетов."""
    per = defaultdict(list)
    for o in sorted(os_, key=lambda o: o["s"] or datetime.min, reverse=True):
        if o["svc_checked"] and not o["svc_missing"] and o["units"] and len(per[o["company_id"]]) < 60:
            per[o["company_id"]].append(o["svc_revenue"] / o["units"])
    price = _invoice_fbs_price()
    price.update({k: statistics.median(v) for k, v in per.items() if v})
    _INV_PRICES.clear()
    _INV_PRICES.update(_invoice_price_timeline())
    return price


_INV_PRICES: dict = {}


def _invoice_price_timeline() -> dict:
    """{клиент: [(создан счёт UTC, цена «Отгрузки FBS» за ед.)]} по возрастанию."""
    tl = defaultdict(list)
    for cid, created, dt_, ops in db.fetchall(
            "SELECT company_id, created_ts, date, ops FROM m_invoices ORDER BY date"):
        pr = [op["price"] for op in json.loads(ops or "[]")
              if "отгрузка" in (op.get("name") or "").lower() and op.get("price")]
        if pr:
            tl[cid].append((created or (dt_ + "T23:59:59"), statistics.median(pr)))
    return tl


def _value(o: dict, price: dict) -> float:
    """Стоимость отгрузки заказа для клиента: точная, если карточка проверена.
    Иначе оценка: цена из первого счёта клиента, выставленного после отгрузки
    (цена того периода — после ДС она меняется), а для ещё не выставленных —
    текущая цена клиента по свежим проверенным заказам."""
    if o["svc_checked"]:
        return o["svc_revenue"]
    units = o["units"] or 1
    for ts, p in _INV_PRICES.get(o["company_id"], []):
        if o["shipped_at"] and ts >= o["shipped_at"]:
            return p * units
    return price.get(o["company_id"], 55) * units


def summary() -> dict:
    os_ = orders()
    now = _now_msk()
    today = now.date()
    yest = today - timedelta(days=1)
    week = today - timedelta(days=7)
    created = Counter(o["c"].date() for o in os_ if o["c"])
    shipped = Counter(o["s"].date() for o in os_ if o["s"])
    price = _unit_price(os_)
    rev = defaultdict(float)
    for o in os_:
        if o["s"] and o["status"] in store.SHIPPED:
            rev[o["s"].date()] += _value(o, price)
    open_ = [o for o in os_ if o["status"] in ("NEW", "PRODUCTS_RESERVED", "EQUIPMENT", "READY_TO_SHIP")]
    age = [(now - o["c"]).total_seconds() / 3600 for o in open_ if o["c"]]
    overdue = sum(1 for o in open_ if o["plan_ok"] and o["p"] < now)
    miss = [o for o in os_ if o["svc_missing"]]
    lost = lambda lst: round(sum(price.get(o["company_id"], 55) * (o["units"] or 1) for o in lst))
    miss_today = [o for o in miss if o["s"] and o["s"].date() == today]
    miss_7 = [o for o in miss if o["s"] and o["s"].date() > week]
    month0 = today.replace(day=1)
    return {
        "now": now.strftime("%d.%m.%Y %H:%M"),
        "orders_today": created[today], "orders_yesterday": created[yest],
        "orders_week_ago": created[week],
        "shipped_today": shipped[today], "shipped_yesterday": shipped[yest],
        "revenue_today": round(rev[today]), "revenue_yesterday": round(rev[yest]),
        "revenue_month": round(sum(v for d, v in rev.items() if d >= month0)),
        "orders_month": sum(v for d, v in created.items() if d >= month0),
        "queue": len(open_), "queue_12h": sum(1 for a in age if a > 12),
        "queue_24h": sum(1 for a in age if a > 24), "overdue": overdue,
        "missing_today": len(miss_today), "missing_7d": len(miss_7),
        "lost_today": lost(miss_today), "lost_7d": lost(miss_7), "lost_total": lost(miss),
        "svc_checked": sum(1 for o in os_ if o["svc_checked"]),
        "shipped_total": sum(1 for o in os_ if o["status"] in store.SHIPPED),
    }


def daily(days: int = 60) -> dict:
    os_ = orders()
    comp = companies()
    start = _now_msk().date() - timedelta(days=days - 1)
    ds = [start + timedelta(days=i) for i in range(days)]
    by_src = defaultdict(Counter)
    by_comp = defaultdict(Counter)
    ship = Counter()
    cancel = Counter()
    for o in os_:
        if o["c"] and o["c"].date() >= start:
            d = o["c"].date()
            by_src[o["source"] or "—"][d] += 1
            by_comp[o["company_id"]][d] += 1
            if o["status"] in ("REJECT", "CANCEL"):
                cancel[d] += 1
        if o["s"] and o["s"].date() >= start:
            ship[o["s"].date()] += 1
    plan = store.plan_get()
    plan_line = []
    for d in ds:
        mk = d.strftime("%Y-%m")
        per_month = (plan.get(mk) or {}).get("orders")
        days_in = ((d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        plan_line.append(round(per_month / days_in, 1) if per_month else None)
    lab = [d.strftime("%d.%m") for d in ds]
    total = [sum(by_src[s][d] for s in by_src) for d in ds]
    # тепловая карта: день недели × час, последние 28 дней
    heat = [[0] * 24 for _ in range(7)]
    h0 = _now_msk() - timedelta(days=28)
    for o in os_:
        if o["c"] and o["c"] >= h0:
            heat[o["c"].weekday()][o["c"].hour] += 1
    return {
        "labels": lab, "total": total, "shipped": [ship[d] for d in ds],
        "cancel": [cancel[d] for d in ds], "plan": plan_line,
        "by_source": {s: [by_src[s][d] for d in ds] for s in by_src},
        "by_company": {_short(comp.get(k, {}).get("name") or str(k)): [by_comp[k][d] for d in ds]
                       for k in sorted(by_comp, key=lambda k: -sum(by_comp[k].values()))},
        "heat": heat,
    }


def speed(days: int = 30) -> dict:
    os_ = orders()
    comp = companies()
    start = _now_msk().date() - timedelta(days=days - 1)
    per_day = defaultdict(list)
    sla = defaultdict(lambda: [0, 0])
    per_comp = defaultdict(list)
    sla_comp = defaultdict(lambda: [0, 0])
    for o in os_:
        if not (o["s"] and o["c"]) or o["s"].date() < start:
            continue
        h = (o["s"] - o["c"]).total_seconds() / 3600
        if h < 0:
            continue
        d = o["s"].date()
        per_day[d].append(h)
        per_comp[o["company_id"]].append(h)
        if o["plan_ok"]:
            ok = o["s"] <= o["p"]
            sla[d][0 if ok else 1] += 1
            sla_comp[o["company_id"]][0 if ok else 1] += 1
    ds = [start + timedelta(days=i) for i in range(days)]
    all_h = [h for v in per_day.values() for h in v]
    on, late = sum(v[0] for v in sla.values()), sum(v[1] for v in sla.values())
    # этапы из журнала статусов (копится с момента запуска синхронизации)
    stages = defaultdict(list)
    log = defaultdict(dict)
    for oid, st, seen in db.fetchall("SELECT order_id, status, seen_at FROM m_status_log"):
        log[oid][st] = _dt(seen)
    pairs = [("NEW", "EQUIPMENT", "Ожидание до сборки"), ("EQUIPMENT", "READY_TO_SHIP", "Сборка"),
             ("READY_TO_SHIP", "SHIPPED", "Готов → отгружен")]
    for st in log.values():
        for a, b, name in pairs:
            if a in st and b in st and st[b] > st[a]:
                stages[name].append((st[b] - st[a]).total_seconds() / 3600)
    return {
        "labels": [d.strftime("%d.%m") for d in ds],
        "median": [_q(per_day[d], 0.5) for d in ds], "p90": [_q(per_day[d], 0.9) for d in ds],
        "sla_pct": [round(100 * sla[d][0] / sum(sla[d]), 1) if sum(sla[d]) else None for d in ds],
        "overall": {"median": _q(all_h, 0.5), "p90": _q(all_h, 0.9),
                    "on_time_pct": round(100 * on / (on + late), 1) if on + late else None,
                    "late": late, "shipped": len(all_h)},
        "by_company": [{"name": _short(comp.get(k, {}).get("name") or str(k)),
                        "median": _q(v, 0.5), "p90": _q(v, 0.9), "n": len(v),
                        "on_time_pct": round(100 * sla_comp[k][0] / sum(sla_comp[k]), 1) if sum(sla_comp[k]) else None}
                       for k, v in sorted(per_comp.items(), key=lambda kv: -len(kv[1]))],
        "stages": [{"name": n, "median": _q(v, 0.5), "p90": _q(v, 0.9), "n": len(v)}
                   for n, v in stages.items()],
    }


def services() -> dict:
    os_ = orders()
    comp = companies()
    price = _unit_price(os_)
    miss = sorted([o for o in os_ if o["svc_missing"]], key=lambda o: o["s"] or datetime.min, reverse=True)
    by_comp = Counter(o["company_id"] for o in miss)
    by_wd = Counter(o["s"].weekday() for o in miss if o["s"])
    return {
        "checked": sum(1 for o in os_ if o["svc_checked"]),
        "backlog": sum(1 for o in os_ if o["status"] in store.SHIPPED and not o["svc_checked"]),
        "missing": len(miss),
        "lost": round(sum(price.get(o["company_id"], 55) * (o["units"] or 1) for o in miss)),
        "rows": [{"number": o["number"], "id": o["id"], "company": _short(comp.get(o["company_id"], {}).get("name") or ""),
                  "status": o["status"], "shipped": o["s"].strftime("%d.%m %H:%M") if o["s"] else "",
                  "units": o["units"], "lost": round(price.get(o["company_id"], 55) * (o["units"] or 1))}
                 for o in miss[:300]],
        "by_company": [{"name": _short(comp.get(k, {}).get("name") or str(k)), "n": v} for k, v in by_comp.most_common()],
        "by_weekday": [by_wd[i] for i in range(7)],
        "unit_price": {_short(comp.get(k, {}).get("name") or str(k)): round(v, 2) for k, v in price.items()},
    }


def clients() -> list[dict]:
    os_ = orders()
    comp = companies()
    today = _now_msk().date()
    inv = db.fetchall("SELECT company_id, total, paid FROM m_invoices")
    billed, paid = defaultdict(float), defaultdict(float)
    for c, t, p in inv:
        billed[c] += t or 0
        paid[c] += p or 0
    out = []
    ids = {o["company_id"] for o in os_} | set(billed)
    total30 = sum(1 for o in os_ if o["c"] and (today - o["c"].date()).days < 30) or 1
    for k in ids:
        mine = [o for o in os_ if o["company_id"] == k and o["c"]]
        w = lambda a, b: sum(1 for o in mine if a <= (today - o["c"].date()).days < b)
        last = max((o["c"] for o in mine), default=None)
        rev30 = sum(o["svc_revenue"] for o in mine if o["s"] and (today - o["s"].date()).days < 30)
        cur7, prev7 = w(0, 7), w(7, 14)
        out.append({
            "id": k, "name": _short(comp.get(k, {}).get("name") or str(k)),
            "full_name": comp.get(k, {}).get("name") or "",
            "orders_7": cur7, "orders_prev7": prev7, "change_7": _pct(cur7, prev7),
            "orders_30": w(0, 30), "share_30": round(100 * w(0, 30) / total30, 1),
            "revenue_30": round(rev30), "billed": round(billed[k]), "paid": round(paid[k]),
            "debt": round(billed[k] - paid[k]),
            "last_order": last.strftime("%d.%m %H:%M") if last else "",
            "alert": bool(prev7 >= 20 and cur7 < prev7 * 0.7),
        })
    return sorted(out, key=lambda r: -r["orders_30"])


def _month_days(mk: str) -> tuple[date, date]:
    d0 = datetime.strptime(mk + "-01", "%Y-%m-%d").date()
    d1 = (d0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return d0, d1


def recurring_for_month(mk: str, full: bool = False) -> dict:
    """Начисление постоянных платежей в месяц, пропорционально дням действия.
    full=True — за весь месяц (для прогноза), иначе по сегодняшний день
    не ограничиваем: расход месяца признаётся целиком, как по договору."""
    d0, d1 = _month_days(mk)
    days_in = (d1 - d0).days + 1
    out = defaultdict(float)
    for r in store.recurring_list():
        s_ = date.fromisoformat(r["start_date"])
        e_ = date.fromisoformat(r["end_date"]) if r["end_date"] else d1
        a, b = max(d0, s_), min(d1, e_)
        if a > b:
            continue
        out[r["category"]] += r["amount"] * ((b - a).days + 1) / days_in
    return out


def invoices() -> list[dict]:
    """Счета МПФИТ с учётом оплат из журнала: если клиент оплатил, а в МПФИТ
    счёт не отмечен, оплату фиксируют в журнале со ссылкой на номер счёта."""
    comp = companies()
    led = defaultdict(float)
    for e in store.ledger_list():
        if e["kind"] == "client_payment" and e.get("invoice"):
            led[e["invoice"]] += e["amount"]
    out = []
    for i, num, cid, st, dt_, tot, paid, created in db.fetchall(
            "SELECT id, number, company_id, status, date, total, paid, created_ts FROM m_invoices "
            "ORDER BY date DESC"):
        eff = max(paid or 0, led.get(num, 0))
        out.append({"id": i, "number": num, "company": _short(comp.get(cid, {}).get("name") or str(cid)),
                    "company_id": cid, "date": dt_, "created": created, "total": round(tot or 0, 2),
                    "paid_mpfit": round(paid or 0, 2), "paid": round(min(eff, tot or 0), 2),
                    "debt": round(max(0, (tot or 0) - eff), 2), "mpfit_status": st,
                    "paid_by_ledger": led.get(num, 0) > (paid or 0)})
    return out


def finance(months: int = 6) -> dict:
    os_ = orders()
    comp = companies()
    today = _now_msk().date()
    mks = []
    d = today.replace(day=1)
    for _ in range(months):
        mks.append(d.strftime("%Y-%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    mks.reverse()
    price = _unit_price(os_)
    # выручка по начислению: отгруженные заказы месяца × цена клиента
    fbs, fbs_est, svc_cost = defaultdict(float), defaultdict(float), defaultdict(float)
    for o in os_:
        if o["s"] and o["status"] in store.SHIPPED:
            mk = o["s"].strftime("%Y-%m")
            v = _value(o, price)
            fbs[mk] += v
            if not o["svc_checked"]:
                fbs_est[mk] += v
            svc_cost[mk] += o["svc_cost"] if o["svc_checked"] else 0
    inv = invoices()
    billed, paid, other = defaultdict(float), defaultdict(float), defaultdict(float)
    for i in inv:
        mk = (i["date"] or "")[:7]
        billed[mk] += i["total"]
        paid[mk] += i["paid"]
    for dt_, ops in db.fetchall("SELECT date, ops FROM m_invoices"):
        for op in json.loads(ops or "[]"):
            n = (op.get("name") or "").lower()
            if "отгрузка" not in n:     # приёмка, хранение, разбор коробов и т.п.
                other[(dt_ or "")[:7]] += op.get("total") or 0
    # расходы: разовые из журнала + постоянные платежи начислением
    one_off = defaultdict(lambda: defaultdict(float))
    inv_ = defaultdict(float)
    cash = {"cash": 0.0, "rs": 0.0}
    for e in store.ledger_list():
        mk = e["date"][:7]
        if e["kind"] == "expense" and e["in_ff"] and not e.get("recurring_id"):
            one_off[mk][e["category"]] += e["amount"]
        if e["kind"] == "investment" and e["in_ff"]:
            inv_[mk] += e["amount"]
        if e["method"] in cash:
            if e["kind"] == "investment" and e["method"] == "personal":
                continue
            sign = 1 if e["kind"] in ("funding", "client_payment", "other_income") else -1
            cash[e["method"]] += sign * e["amount"]
    rec = {mk: recurring_for_month(mk) for mk in mks}
    cats = {"Налоги"}
    for mk in mks:
        cats |= set(one_off[mk]) | set(rec[mk])
    rows = []
    for mk in mks:
        by_cat = defaultdict(float)
        for c, v in one_off[mk].items():
            by_cat[c] += v
        for c, v in rec[mk].items():
            by_cat[c] += v
        revenue = fbs[mk] + other[mk]
        tax = revenue * store.TAX_RATE          # налог к удержанию с выручки
        by_cat["Налоги"] += tax
        e_tot = sum(by_cat.values())
        rows.append({
            "month": mk, "revenue": round(revenue), "fbs": round(fbs[mk]), "fbs_est": round(fbs_est[mk]),
            "other": round(other[mk]), "billed": round(billed[mk]), "paid": round(paid[mk]),
            "svc_cost": round(svc_cost[mk]),
            "recurring": round(sum(rec[mk].values())), "tax": round(tax), "one_off": round(sum(one_off[mk].values())),
            "expenses": round(e_tot), "by_cat": {c: round(by_cat.get(c, 0)) for c in cats},
            "investments": round(inv_[mk]),
            "profit": round(revenue - e_tot),
            "margin": round(100 * (revenue - e_tot) / revenue) if revenue else None,
        })
    # ожидаемая выручка: отгружено после последнего счёта клиента (ещё не выставлено)
    # + выставлено, но не оплачено
    last_inv = {}
    for i in inv:
        ts = i["created"] or (i["date"] + "T23:59:59")
        last_inv[i["company_id"]] = max(last_inv.get(i["company_id"], ""), ts)
    unbilled = defaultdict(float)
    for o in os_:
        if o["shipped_at"] and o["status"] in store.SHIPPED:
            cut = last_inv.get(o["company_id"], "")
            if o["shipped_at"] > cut:
                unbilled[o["company_id"]] += _value(o, price)
    # хранение после последнего счёта тоже войдёт в следующий счёт
    for cid, dt_, amt in db.fetchall("SELECT company_id, date, amount FROM m_storage"):
        if dt_ > (last_inv.get(cid) or "")[:10]:
            unbilled[cid] += amt or 0
    unpaid = defaultdict(float)
    for i in inv:
        unpaid[i["company_id"]] += i["debt"]
    exp_rows = []
    for cid in set(unbilled) | {k for k, v in unpaid.items() if v}:
        exp_rows.append({"company": _short(comp.get(cid, {}).get("name") or str(cid)),
                         "unbilled": round(unbilled[cid]), "unpaid": round(unpaid[cid]),
                         "total": round(unbilled[cid] + unpaid[cid]),
                         "since": (last_inv.get(cid) or "")[:10]})
    exp_rows.sort(key=lambda r: -r["total"])
    cats_sorted = sorted(cats, key=lambda c: -sum(r["by_cat"].get(c, 0) for r in rows))
    return {"months": rows, "categories": cats_sorted, "cash": {k: round(v, 2) for k, v in cash.items()},
            "billed_total": round(sum(i["total"] for i in inv)), "paid_total": round(sum(i["paid"] for i in inv)),
            "debt": round(sum(i["debt"] for i in inv)),
            "expected": {"unbilled": round(sum(unbilled.values())), "unpaid": round(sum(unpaid.values())),
                         "total": round(sum(unbilled.values()) + sum(unpaid.values())), "rows": exp_rows},
            "estimated_share": round(100 * sum(r["fbs_est"] for r in rows) / max(1, sum(r["fbs"] for r in rows)))}


def plan_fact() -> dict:
    """План/факт текущего месяца + прогноз по темпу."""
    today = _now_msk().date()
    mk = today.strftime("%Y-%m")
    days_in = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    passed = today.day
    s = summary()
    fin = {r["month"]: r for r in finance(2)["months"]}.get(mk, {})
    fact = {"orders": s["orders_month"], "revenue": fin.get("revenue", 0),
            "expenses": fin.get("expenses", 0)}
    rec_full = sum(recurring_for_month(mk).values())
    one_off = fin.get("one_off", 0)
    fact["profit"] = fact["revenue"] - fact["expenses"]
    plan = store.plan_get().get(mk, {})
    rows = []
    for key, name in (("orders", "Заказы, шт"), ("revenue", "Выручка, ₽"),
                      ("expenses", "Расходы, ₽"), ("profit", "Прибыль, ₽")):
        f = fact[key]
        forecast = round(f / passed * days_in) if passed else None
        if key == "expenses":      # постоянные платежи уже начислены за весь месяц
            forecast = round(one_off / passed * days_in + rec_full
                             + fact["revenue"] / passed * days_in * store.TAX_RATE) if passed else None
        if key == "profit":
            forecast = None        # досчитаем ниже из прогнозов выручки и расходов
        p = plan.get(key)
        rows.append({"key": key, "name": name, "plan": p, "fact": round(f), "forecast": forecast,
                     "done_pct": round(100 * f / p) if p else None,
                     "forecast_pct": round(100 * forecast / p) if p and forecast is not None else None})
    byk = {r["key"]: r for r in rows}
    pr = byk["profit"]
    if byk["revenue"]["forecast"] is not None and byk["expenses"]["forecast"] is not None:
        pr["forecast"] = byk["revenue"]["forecast"] - byk["expenses"]["forecast"]
        if pr["plan"]:
            pr["forecast_pct"] = round(100 * pr["forecast"] / pr["plan"])
    return {"month": mk, "days_passed": passed, "days_in_month": days_in, "rows": rows}
