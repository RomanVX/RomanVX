"""Банковская выписка Альфы (xlsx) → таблица bank_tx.

Выписка — источник правды по расчётному счёту: остаток, поступления от клиентов
и все списания. Записи журнала с методом «rs» за период выписки перекрываются ею."""
import hashlib
import io
import warnings
from datetime import datetime

import store

# ИНН клиентов: оплата счетов МПФИТ
CLIENT_INN = {"391900761608", "391429672864", "7715772939", "7743003388"}
OWNER_INN = "774300338874"        # ИП Калятин: переводы «собственных средств» = вывод владельцу


def classify(party: str, inn: str, purpose: str, credit: bool) -> tuple[str, str]:
    """(вид, статья). Вид: client_payment | owner | expense | income_other."""
    p, n = (purpose or "").lower(), (party or "").lower()
    if inn == OWNER_INN:
        return ("owner_in" if credit else "owner", "Вывод владельцу" if not credit else "Внесение владельцем")
    if credit:
        return ("client_payment", "Оплата от клиента") if (inn in CLIENT_INN or "счет" in p or "счёт" in p) \
            else ("income_other", "Прочий доход")
    if "печатный двор" in n or "аренд" in p or "парков" in p or "въезд" in p:
        return "expense", "Аренда"
    if "мтс" in n or "лицевого счета" in p or "связ" in p:
        return "expense", "Связь"
    if "видео" in n or "оборудован" in p:
        return "expense", "Оборудование"
    if "упаковоч" in p:
        return "expense", "Расходники и упаковка"
    if "зарплат" in p or "заработн" in p:
        return "expense", "ФОТ"
    if "налог" in p or "усн" in p or "фнс" in n:
        return "expense", "Налоги"
    return "expense", "Прочее"


def _num(v) -> float:
    if v in (None, ""):
        return 0.0
    return float(str(v).replace(" ", "").replace(",", "."))


def parse(data: bytes) -> dict:
    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).active
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    meta = {"account": "", "opening": None, "closing": None, "start": None, "end": None}
    head = None
    for i, r in enumerate(rows):
        a = str(r[0] or "")
        if a.startswith("Выписка по счёту"):
            meta["account"] = str(r[1] or "")
        elif a == "За период":
            import re
            m = re.findall(r"\d{2}\.\d{2}\.\d{4}", str(r[1] or ""))
            if len(m) == 2:
                meta["start"] = datetime.strptime(m[0], "%d.%m.%Y").date().isoformat()
                meta["end"] = datetime.strptime(m[1], "%d.%m.%Y").date().isoformat()
        elif a == "Остаток входящий":
            meta["opening"] = _num(r[1])
        elif a == "Остаток исходящий":
            meta["closing"] = _num(r[1])
        elif a == "Дата" and "Дебет" in [str(x) for x in r]:
            head = i
    if head is None or meta["opening"] is None:
        raise ValueError("не похоже на выписку Альфы")
    tx = []
    for r in rows[head + 2:]:
        try:
            d = datetime.strptime(str(r[0]).strip(), "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue
        deb, cred = _num(r[2]), _num(r[3])
        if not deb and not cred:
            continue
        party, inn, purpose = str(r[4] or ""), str(r[5] or ""), str(r[10] or "")
        kind, cat = classify(party, inn, purpose, bool(cred))
        amt = cred - deb
        i = hashlib.md5(f"{meta['account']}|{d}|{r[1]}|{amt}|{inn}".encode()).hexdigest()[:16]
        tx.append((i, d, amt, party, inn, purpose, kind, cat))
    return {"meta": meta, "tx": tx}


def import_statement(data: bytes) -> dict:
    res = parse(data)
    meta = res["meta"]
    store.bank_upsert(res["tx"])
    cur = store.kv_get("bank_meta") or {}
    # входящий остаток берём из самой ранней выписки
    if not cur.get("start") or meta["start"] <= cur["start"]:
        cur.update({"start": meta["start"], "opening": meta["opening"]})
    if not cur.get("end") or meta["end"] >= cur["end"]:
        cur.update({"end": meta["end"], "closing_stated": meta["closing"]})
    cur["account"] = meta["account"]
    store.kv_set("bank_meta", cur)
    return {"imported": len(res["tx"]), "period": [meta["start"], meta["end"]],
            "opening": meta["opening"], "closing": meta["closing"],
            "check": round(store.bank_balance(), 2)}
