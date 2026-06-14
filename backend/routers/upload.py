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


def _parse_csv(content: bytes) -> tuple[dict[str, float], dict[str, str]]:
    import csv
    mapping: dict[str, float] = {}
    names:   dict[str, str]   = {}
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header_skipped = False
    for row in reader:
        if not header_skipped:
            header_skipped = True
            continue
        if len(row) < 2:
            continue
        try:
            article = row[0].strip()
            cost = float(row[1].replace(",", ".").replace(" ", ""))
            if article and cost > 0:
                mapping[article] = cost
        except (ValueError, IndexError):
            pass
    return mapping, names


@router.post("/costs")
async def upload_costs(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    content = await file.read()

    if name.endswith(".xlsx") or name.endswith(".xls"):
        mapping, names = _parse_xlsx(content)
    elif name.endswith(".csv"):
        mapping, names = _parse_csv(content)
    else:
        raise HTTPException(400, "Формат не поддерживается. Загрузите .xlsx или .csv")

    if not mapping:
        raise HTTPException(422, "Не удалось распознать данные. "
                                 "Убедитесь, что файл содержит колонки "
                                 "'Артикул продавца' и 'Себестоимость ед.'")

    cost_store.set_costs(mapping, names)
    _log.info("Costs loaded: %d articles, %d names", len(mapping), len(names))
    return {"loaded": len(mapping), "names_loaded": len(names),
            "sample": dict(list(mapping.items())[:3])}


@router.get("/costs/status")
async def costs_status():
    return {"loaded": cost_store.count()}
