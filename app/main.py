"""Web API and static UI for the grocery receipt tracker."""

import csv
import io
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import extract
from .categories import FALLBACK_CATEGORY
from .db import connect, get_categories, load_corrections, remember_correction

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MONTH_PATTERN = r"^\d{4}-\d{2}$"

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


class Receipt(BaseModel):
    store: str | None = None
    purchased_on: date
    currency: str = "USD"
    tax: float = 0
    total: float | None = None
    items: list[Item] = Field(min_length=1)


class ReceiptUpdate(BaseModel):
    store: str | None = None
    purchased_on: date | None = None
    tax: float | None = None
    total: float | None = None


class ItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    quantity: float | None = None
    price: float | None = None
    category: str | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=300)

    @field_validator("name", "description")
    @classmethod
    def strip(cls, v: str) -> str:
        return " ".join(v.split())


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("name", "description")
    @classmethod
    def strip(cls, v: str | None) -> str | None:
        return None if v is None else " ".join(v.split())


def require_categories(conn, names) -> None:
    known = get_categories(conn)
    unknown = sorted({n for n in names if n not in known})
    if unknown:
        raise HTTPException(422, f"Unknown category: {', '.join(unknown)}")


def get_category_row(conn, category_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Category not found.")
    return row


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---- Categories ----

@app.get("/api/categories")
def list_categories():
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.description, COUNT(i.id) AS item_count
               FROM categories c LEFT JOIN items i ON i.category = c.name
               GROUP BY c.id ORDER BY c.id"""
        ).fetchall()
    return [{**dict(r), "locked": r["name"] == FALLBACK_CATEGORY} for r in rows]


@app.post("/api/categories", status_code=201)
def create_category(category: CategoryIn):
    with connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO categories (name, description) VALUES (?, ?)", (category.name, category.description)
            )
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"A category called {category.name!r} already exists.") from e
    return {"id": cur.lastrowid, **category.model_dump()}


@app.patch("/api/categories/{category_id}")
def update_category(category_id: int, update: CategoryUpdate):
    with connect() as conn:
        row = get_category_row(conn, category_id)
        old_name = row["name"]
        new_name = update.name if update.name is not None else old_name
        if new_name != old_name and old_name == FALLBACK_CATEGORY:
            raise HTTPException(400, f"{FALLBACK_CATEGORY!r} can't be renamed.")
        description = update.description if update.description is not None else row["description"]
        try:
            conn.execute(
                "UPDATE categories SET name = ?, description = ? WHERE id = ?", (new_name, description, category_id)
            )
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"A category called {new_name!r} already exists.") from e
        if new_name != old_name:
            conn.execute("UPDATE items SET category = ? WHERE category = ?", (new_name, old_name))
            conn.execute("UPDATE corrections SET category = ? WHERE category = ?", (new_name, old_name))
    return {"id": category_id, "name": new_name, "description": description}


@app.delete("/api/categories/{category_id}", status_code=204)
def delete_category(category_id: int, move_to: str = Query(FALLBACK_CATEGORY)):
    """Delete a category, moving its items (and remembered corrections) to another one."""
    with connect() as conn:
        name = get_category_row(conn, category_id)["name"]
        if name == FALLBACK_CATEGORY:
            raise HTTPException(400, f"{FALLBACK_CATEGORY!r} can't be deleted.")
        if move_to == name:
            raise HTTPException(400, "Choose a different category to move the items to.")
        require_categories(conn, [move_to])
        conn.execute("UPDATE items SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("UPDATE corrections SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


# ---- Scanning and receipts ----

@app.post("/api/scan")
async def scan(image: UploadFile = File(...)):
    """Read a receipt photo and return a draft for the user to review. Nothing is saved."""
    data = await image.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image is larger than 10 MB.")
    with connect() as conn:
        categories = get_categories(conn)
    try:
        return extract.extract_receipt(
            data, image.content_type or "", categories, corrections=load_corrections()
        )
    except extract.ExtractionError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/receipts", status_code=201)
def save_receipt(receipt: Receipt):
    with connect() as conn:
        require_categories(conn, [i.category for i in receipt.items])
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
def list_receipts(month: str | None = Query(None, pattern=MONTH_PATTERN)):
    where, params = _date_filter(None, None, month)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT r.*, COUNT(i.id) AS item_count, COALESCE(SUM(i.price), 0) AS items_total
                FROM receipts r LEFT JOIN items i ON i.receipt_id = r.id{where}
                GROUP BY r.id ORDER BY r.purchased_on DESC, r.id DESC""",
            params,
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


@app.patch("/api/receipts/{receipt_id}")
def update_receipt(receipt_id: int, update: ReceiptUpdate):
    fields = update.model_dump(exclude_unset=True)
    if "purchased_on" in fields:
        if fields["purchased_on"] is None:
            raise HTTPException(422, "A receipt needs a date.")
        fields["purchased_on"] = fields["purchased_on"].isoformat()
    if "tax" in fields and fields["tax"] is None:
        fields["tax"] = 0
    if not fields:
        raise HTTPException(400, "Nothing to update.")
    with connect() as conn:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE receipts SET {assignments} WHERE id = ?", (*fields.values(), receipt_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Receipt not found.")
        row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    return dict(row)


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
        if "category" in fields:
            require_categories(conn, [fields["category"]])
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


# ---- Reporting ----

def _date_filter(start: date | None, end: date | None, month: str | None = None) -> tuple[str, list]:
    clauses, params = [], []
    if start:
        clauses.append("r.purchased_on >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("r.purchased_on <= ?")
        params.append(end.isoformat())
    if month:
        clauses.append("substr(r.purchased_on, 1, 7) = ?")
        params.append(month)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


@app.get("/api/months")
def months():
    """One row per month that has receipts, newest first."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT substr(r.purchased_on, 1, 7) AS month,
                      COUNT(*) AS receipt_count,
                      COALESCE(SUM(r.tax), 0) AS tax,
                      COALESCE(SUM((SELECT SUM(price) FROM items WHERE receipt_id = r.id)), 0) AS items_total
               FROM receipts r GROUP BY month ORDER BY month DESC"""
        ).fetchall()
    return [{**dict(r), "items_total": round(r["items_total"], 2), "tax": round(r["tax"], 2)} for r in rows]


@app.get("/api/summary")
def summary(
    start: date | None = Query(None),
    end: date | None = Query(None),
    month: str | None = Query(None, pattern=MONTH_PATTERN),
):
    """Spending per category for receipts in a month (YYYY-MM) or between start and end."""
    where, params = _date_filter(start, end, month)
    with connect() as conn:
        by_category = conn.execute(
            f"""SELECT i.category, SUM(i.price) AS total, COUNT(*) AS item_count
                FROM items i JOIN receipts r ON r.id = i.receipt_id{where}
                GROUP BY i.category ORDER BY total DESC""",
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
    }


@app.get("/api/items")
def list_items(
    category: str | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    month: str | None = Query(None, pattern=MONTH_PATTERN),
):
    where, params = _date_filter(start, end, month)
    if category:
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
