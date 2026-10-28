"""SQLite storage for receipts and their line items."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .categories import DEFAULT_CATEGORIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id           INTEGER PRIMARY KEY,
    store        TEXT,
    purchased_on TEXT NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    tax          REAL NOT NULL DEFAULT 0,
    total        REAL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    raw_text   TEXT NOT NULL DEFAULT '',
    quantity   REAL NOT NULL DEFAULT 1,
    price      REAL NOT NULL,
    category   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_receipt ON items(receipt_id);
CREATE INDEX IF NOT EXISTS receipts_date ON receipts(purchased_on);
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);
-- Names and categories the user fixed by hand, keyed by the line as printed on the receipt.
CREATE TABLE IF NOT EXISTS corrections (
    raw_text   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db_path() -> Path:
    return Path(os.environ.get("GROCERY_DB_PATH", "data/groceries.db"))


@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO categories (name, description) VALUES (?, ?)", DEFAULT_CATEGORIES.items()
            )
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_raw(raw_text: str) -> str:
    return " ".join(raw_text.upper().split())


def remember_correction(conn, raw_text: str, name: str, category: str) -> None:
    key = normalize_raw(raw_text)
    if not key:
        return
    conn.execute(
        """INSERT INTO corrections (raw_text, name, category) VALUES (?, ?, ?)
           ON CONFLICT(raw_text) DO UPDATE SET name = excluded.name, category = excluded.category,
                                               updated_at = datetime('now')""",
        (key, name, category),
    )


def load_corrections(limit: int = 300) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT raw_text, name, category FROM corrections ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {r["raw_text"]: {"name": r["name"], "category": r["category"]} for r in rows}


def get_categories(conn) -> dict[str, str]:
    """Category name -> description, in the order they were created."""
    return {r["name"]: r["description"] for r in conn.execute("SELECT name, description FROM categories ORDER BY id")}
