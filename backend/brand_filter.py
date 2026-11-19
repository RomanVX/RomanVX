"""Фильтр по бренду для любых разделов дашборда (параметр ?brand=… у GET /api/*).

Ответ раздела проходится рекурсивно: в каждом списке товаров (объекты с артикулом)
остаются только товары выбранного бренда (бренд — по каталогу кабинета, иначе
поле brand/group у самого товара). Итоги рядом со списком пересчитываются по
оставшимся товарам:
  • total_X (число или массив по неделям) = сумма X у товаров;
  • totals {X: число} = сумма X; производные ДРР/ROAS/CTR/маржа — заново;
  • totals {месяц: {X: число}} при товарах с months {месяц: {X}} — по месяцам.
Отчёты-«статьи» (P&L) товаров не содержат и не меняются — для них фронт
показывает P&L бренда из юнит-экономики.
"""
import json

ART_KEYS = ("sku", "offer", "art", "article", "supplierArticle", "vendorCode",
            "offer_id", "sa", "nmId", "nm", "nm_id")
_SKIP_LIST_KEYS = {"months", "weeks", "labels", "dates", "periods", "chart"}


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _brand_of(item: dict) -> str:
    import catalog as _cat
    for k in ART_KEYS:
        v = item.get(k)
        if v in (None, "", 0):
            continue
        v = str(v).strip()
        cands = [v]
        if v.isdigit():                 # nmId WB или SKU Ozon → артикул
            cands = [_cat.resolve_wb(v), _cat.resolve_ozon(v), v]
        for a in cands:
            b = (_cat.lookup(a) or {}).get("brand") or ""
            if b and b != "Прочее":
                return b
    for k in ("brand", "group", "brandName"):
        if item.get(k):
            return str(item[k])
    return "Прочее"


def _is_item_list(lst) -> bool:
    if not isinstance(lst, list) or not lst:
        return False
    sample = [x for x in lst[:50] if isinstance(x, dict)]
    if len(sample) < max(1, len(lst[:50]) // 2):
        return False
    hits = sum(1 for x in sample if any(x.get(k) not in (None, "", 0) for k in ART_KEYS))
    return hits >= max(1, len(sample) // 2)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _recalc(d: dict, items: list) -> None:
    """Пересчёт итогов словаря d по отфильтрованному списку items."""
    for k, v in list(d.items()):
        if not k.startswith("total_"):
            continue
        src = k[len("total_"):]
        if _num(v):
            if any(_num(it.get(src)) for it in items):
                d[k] = round(sum(it.get(src) or 0 for it in items if _num(it.get(src))), 2)
        elif isinstance(v, list) and v and all(_num(x) for x in v):
            arrs = [it.get(src) for it in items if isinstance(it.get(src), list)]
            if arrs:
                d[k] = [round(sum((a[i] if i < len(a) and _num(a[i]) else 0) for a in arrs), 2)
                        for i in range(len(v))]
    tot = d.get("totals")
    if isinstance(tot, dict) and tot:
        vals = list(tot.values())
        if all(_num(x) or x is None for x in vals):
            for k in list(tot):
                if any(_num(it.get(k)) for it in items):
                    tot[k] = round(sum(it.get(k) or 0 for it in items if _num(it.get(k))), 2)
            _derive(tot)
        elif all(isinstance(x, dict) for x in vals) and any(isinstance(it.get("months"), dict) for it in items):
            for mk, cols in tot.items():
                for c in list(cols):
                    if c == "margin":
                        continue
                    if any(_num(((it.get("months") or {}).get(mk) or {}).get(c)) for it in items):
                        cols[c] = round(sum(((it.get("months") or {}).get(mk) or {}).get(c) or 0
                                            for it in items), 2)
                if "margin" in cols and _num(cols.get("revenue")) and cols.get("revenue"):
                    g = cols.get("gross")
                    if _num(g):
                        cols["margin"] = round(g / cols["revenue"] * 100)


def _derive(t: dict) -> None:
    def g(k):
        v = t.get(k)
        return v if _num(v) else None
    if "drr" in t and g("ad") is not None and g("rev"):
        t["drr"] = round(g("ad") / g("rev") * 100, 1)
    if "drr_prev" in t and g("ad_prev") is not None and g("rev_prev"):
        t["drr_prev"] = round(g("ad_prev") / g("rev_prev") * 100, 1)
    if "roas" in t and g("ad"):
        base = g("ad_rev") if g("ad_rev") is not None else g("orders_money")
        if base is not None:
            t["roas"] = round(base / g("ad"), 2)
    if "ctr" in t and g("views"):
        t["ctr"] = round((g("clicks") or 0) / g("views") * 100, 2)


def apply(obj, brand: str):
    """Фильтрует ответ на месте; возвращает obj."""
    want = _norm(brand)

    def walk(o):
        if isinstance(o, dict):
            filtered = []
            for k, v in list(o.items()):
                if k in _SKIP_LIST_KEYS:
                    continue
                if _is_item_list(v):
                    keep = [x for x in v if not isinstance(x, dict) or _norm(_brand_of(x)) == want]
                    o[k] = keep
                    filtered.append(keep)
                    for x in keep:
                        walk(x)
                else:
                    walk(v)
            if filtered:
                _recalc(o, max(filtered, key=len))
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    if isinstance(obj, dict):
        obj["brand_filter"] = brand
    return obj


def filter_body(body: bytes, brand: str) -> bytes | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return json.dumps(apply(data, brand), ensure_ascii=False).encode()
