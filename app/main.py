"""Web API and static UI for the grocery receipt tracker."""

import csv
import io
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import extract
from .categories import CATEGORIES, CATEGORY_NAMES
from .db import connect, load_corrections, remember_correction

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Grocery Reader")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Item(BaseModel):
    name: str = Field(min_length=1)
    raw_text: str = ""
    quantity: float = 1
    price: float
    category: str
    # Set by the UI when the user changed the name or category Claude suggested.
    corrected: bool = False

    @field_validator("category")
    @classmethod
    def known_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v


class Receipt(BaseModel):
    store: str | None = None
    purchased_on: date
    currency: str = "USD"
    tax: float = 0
    total: float | None = None
    items: list[Item] = Field(min_length=1)


class ItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    quantity: float | None = None
    price: float | None = None
    category: str | None = None

    @field_validator("category")
    @classmethod
    def known_category(cls, v: str | None) -> str | None:
        if v is not None and v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/categories")
def categories():
    return [{"name": name, "description": desc} for name, desc in CATEGORIES.items()]


@app.post("/api/scan")
async def scan(image: UploadFile = File(...)):
    """Read a receipt photo and return a draft for the user to review. Nothing is saved."""
    data = await image.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image is larger than 10 MB.")
    try:
        return extract.extract_receipt(data, image.content_type or "", corrections=load_corrections())
    except extract.ExtractionError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/receipts", status_code=201)
def save_receipt(receipt: Receipt):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO receipts (store, purchased_on, currency, tax, total) VALUES (?, ?, ?, ?, ?)",
            (receipt.store, receipt.purchased_on.isoformat(), receipt.currency, receipt.tax, receipt.total),
        )
        receipt_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO items (receipt_id, name, raw_text, quantity, price, category) VALUES (?, ?, ?, ?, ?, ?)",
            [(receipt_id, i.name, i.raw_text, i.quantity, i.price, i.category) for i in receipt.items],
        )
        for i in receipt.items:
            if i.corrected:
                remember_correction(conn, i.raw_text, i.name, i.category)
    return {"id": receipt_id}


@app.get("/api/receipts")
def list_receipts():
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, COUNT(i.id) AS item_count, COALESCE(SUM(i.price), 0) AS items_total
               FROM receipts r LEFT JOIN items i ON i.receipt_id = r.id
               GROUP BY r.id ORDER BY r.purchased_on DESC, r.id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/receipts/{receipt_id}")
def get_receipt(receipt_id: int):
    with connect() as conn:
        receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if receipt is None:
            raise HTTPException(404, "Receipt not found.")
        items = conn.execute("SELECT * FROM items WHERE receipt_id = ? ORDER BY id", (receipt_id,)).fetchall()
    return {**dict(receipt), "items": [dict(i) for i in items]}


@app.delete("/api/receipts/{receipt_id}", status_code=204)
def delete_receipt(receipt_id: int):
    with connect() as conn:
        if conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,)).rowcount == 0:
            raise HTTPException(404, "Receipt not found.")


@app.patch("/api/items/{item_id}")
def update_item(item_id: int, update: ItemUpdate):
    fields = update.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Nothing to update.")
    with connect() as conn:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found.")
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if "name" in fields or "category" in fields:
            remember_correction(conn, row["raw_text"], row["name"], row["category"])
    return dict(row)


@app.get("/api/corrections")
def list_corrections():
    return [{"raw_text": raw, **c} for raw, c in load_corrections(limit=10_000).items()]


def _date_filter(start: date | None, end: date | None) -> tuple[str, list]:
    clauses, params = [], []
    if start:
        clauses.append("r.purchased_on >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("r.purchased_on <= ?")
        params.append(end.isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


@app.get("/api/summary")
def summary(start: date | None = Query(None), end: date | None = Query(None)):
    """Spending per category (and per month) for receipts dated between start and end."""
    where, params = _date_filter(start, end)
    with connect() as conn:
        by_category = conn.execute(
            f"""SELECT i.category, SUM(i.price) AS total, COUNT(*) AS item_count
                FROM items i JOIN receipts r ON r.id = i.receipt_id{where}
                GROUP BY i.category ORDER BY total DESC""",
            params,
        ).fetchall()
        by_month = conn.execute(
            f"""SELECT substr(r.purchased_on, 1, 7) AS month, i.category, SUM(i.price) AS total
                FROM items i JOIN receipts r ON r.id = i.receipt_id{where}
                GROUP BY month, i.category ORDER BY month""",
            params,
        ).fetchall()
        totals = conn.execute(
            f"SELECT COUNT(*) AS receipts, COALESCE(SUM(r.tax), 0) AS tax FROM receipts r{where}", params
        ).fetchone()
    items_total = sum(r["total"] for r in by_category)
    return {
        "items_total": round(items_total, 2),
        "tax": round(totals["tax"], 2),
        "receipt_count": totals["receipts"],
        "by_category": [
            {
                "category": r["category"],
                "total": round(r["total"], 2),
                "item_count": r["item_count"],
                "share": r["total"] / items_total if items_total else 0,
            }
            for r in by_category
        ],
        "by_month": [dict(r) for r in by_month],
    }


@app.get("/api/items")
def list_items(
    category: str | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
):
    where, params = _date_filter(start, end)
    if category:
        if category not in CATEGORIES:
            raise HTTPException(400, f"Unknown category. Use one of: {', '.join(CATEGORY_NAMES)}")
        where += (" AND " if where else " WHERE ") + "i.category = ?"
        params.append(category)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.*, r.store, r.purchased_on
                FROM items i JOIN receipts r ON r.id = i.receipt_id{where}
                ORDER BY r.purchased_on DESC, i.price DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/export.csv")
def export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "store", "item", "receipt_text", "quantity", "price", "category", "receipt_id"])
    with connect() as conn:
        for r in conn.execute(
            """SELECT r.purchased_on, r.store, i.name, i.raw_text, i.quantity, i.price, i.category, r.id
               FROM items i JOIN receipts r ON r.id = i.receipt_id ORDER BY r.purchased_on, i.id"""
        ):
            writer.writerow(list(r))
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="grocery-items.csv"'},
    )
