"""File upload endpoints — unit cost + product name table (xlsx/csv)."""
import io
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
import cost_store

router = APIRouter(prefix="/api/upload", tags=["upload"])
_log = logging.getLogger(__name__)


def _parse_xlsx(content: bytes) -> tuple[dict[str, float], dict[str, str]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active

    art_col: int | None  = None
    cost_col: int | None = None
    name_col: int | None = None
    mapping: dict[str, float] = {}
    names:   dict[str, str]   = {}

    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        # Detect header row
        if art_col is None:
            for i, cell in enumerate(row):
                s = str(cell or "").lower().strip()
                if "артикул продавца" in s:
                    art_col = i
                if "себестоимость ед" in s:
                    cost_col = i
                if s == "название":
                    name_col = i
            continue  # skip header row itself

        if art_col is None:
            continue
        if cost_col is None:
            cost_col = 4  # fallback column E

        try:
            article = str(row[art_col] or "").strip()
            if not article or article in ("None", ""):
                continue
            raw = row[cost_col]
            cost = float(str(raw).replace(",", ".").replace(" ", "").replace("\xa0", ""))
            if cost > 0:
                mapping[article] = cost
            if name_col is not None and name_col < len(row):
                n = str(row[name_col] or "").strip()
                if n and n != "None":
                    names[article] = n
        except (ValueError, TypeError, IndexError):
            pass

    wb.close()
    return mapping, names


def _parse_csv(content: bytes) -> tuple[dict[str, float], dict[str, str], dict[str, int]]:
    import csv, re
    mapping: dict[str, float] = {}
    names:   dict[str, str]   = {}
    nmids:   dict[str, int]   = {}
    text = content.decode("utf-8-sig", errors="replace")
    # detect delimiter
    delim = ";" if text.count(";") > text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    for row in reader:
        if not row or not row[0].strip():
            continue
        article = row[0].strip()
        # skip header rows
        if article.lower() in ("артикул", "sku", "артикул продавца", ""):
            continue
        try:
            # Format: SKU ; nmId ; category ; full_name ; short_name ; cost ₽
            # or older: SKU ; cost
            if len(row) >= 6:
                # nmId in col 1
                try:
                    nmids[article] = int(row[1].strip())
                except (ValueError, TypeError):
                    pass
                # short name in col 4, full name in col 3
                short = row[4].strip() if len(row) > 4 else ""
                full  = row[3].strip() if len(row) > 3 else ""
                names[article] = short or full
                raw_cost = row[5]
            else:
                raw_cost = row[1]
            # strip ₽, spaces, nbsp
            raw_cost = re.sub(r"[₽\s\xa0]", "", str(raw_cost)).replace(",", ".")
            cost = float(raw_cost)
            if cost > 0:
                mapping[article] = cost
        except (ValueError, IndexError):
            pass
    return mapping, names, nmids


@router.post("/costs")
async def upload_costs(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    content = await file.read()

    if name.endswith(".xlsx") or name.endswith(".xls"):
        mapping, names = _parse_xlsx(content)
        nmids = {}
    elif name.endswith(".csv"):
        mapping, names, nmids = _parse_csv(content)
    else:
        raise HTTPException(400, "Формат не поддерживается. Загрузите .xlsx или .csv")

    if not mapping:
        raise HTTPException(422, "Не удалось распознать данные. "
                                 "Убедитесь, что файл содержит колонки "
                                 "'Артикул продавца' и 'Себестоимость ед.'")

    cost_store.set_costs(mapping, names, nmids)
    _log.info("Costs loaded: %d articles, %d names, %d nmIds", len(mapping), len(names), len(nmids))
    return {"loaded": len(mapping), "names_loaded": len(names), "nmids_loaded": len(nmids),
            "sample": {k: {"cost": mapping[k], "name": names.get(k)} for k in list(mapping)[:3]}}


@router.get("/costs/status")
async def costs_status():
    return {"loaded": cost_store.count()}


@router.get("/costs/list")
async def costs_list():
    """Полный справочник себестоимостей."""
    return {"items": cost_store.get_all(), "total": cost_store.count()}
