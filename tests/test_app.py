import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import extract, main

DRAFT = {
    "readable": True,
    "store": "Trader Joe's",
    "purchased_on": "2026-10-20",
    "currency": "USD",
    "items": [
        {"name": "Chicken thighs", "raw_text": "CHKN THIGH", "quantity": 1, "price": 8.49, "category": "Meat & Seafood"},
        {"name": "Tortilla chips", "raw_text": "TORT CHIPS", "quantity": 1, "price": 2.99, "category": "Snacks & Sweets"},
        {"name": "Dark chocolate", "raw_text": "DK CHOC", "quantity": 2, "price": 3.98, "category": "Snacks & Sweets"},
    ],
    "tax": 0.55,
    "total": 16.01,
}


class FakeClient:
    """Stands in for anthropic.Anthropic; records the request and returns a canned response."""

    def __init__(self, payload=DRAFT, stop_reason="end_turn"):
        self.calls = []
        text = SimpleNamespace(type="text", text=json.dumps(payload))
        response = SimpleNamespace(stop_reason=stop_reason, content=[text])

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROCERY_DB_PATH", str(tmp_path / "test.db"))
    return TestClient(main.app)


def draft_without_flag():
    return {k: v for k, v in DRAFT.items() if k != "readable"}


def test_extract_sends_image_and_schema():
    fake = FakeClient()
    result = extract.extract_receipt(b"\xff\xd8fake", "image/jpeg", client=fake)
    assert result == draft_without_flag()
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["schema"] is extract.RECEIPT_SCHEMA
    image = call["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"


def test_extract_rejects_unreadable_and_refusals():
    with pytest.raises(extract.ExtractionError, match="readable"):
        extract.extract_receipt(b"x", "image/png", client=FakeClient({**DRAFT, "readable": False, "items": []}))
    with pytest.raises(extract.ExtractionError, match="declined"):
        extract.extract_receipt(b"x", "image/png", client=FakeClient(stop_reason="refusal"))
    with pytest.raises(extract.ExtractionError, match="Unsupported"):
        extract.extract_receipt(b"x", "image/heic", client=FakeClient())


def test_scan_endpoint_returns_draft(client, monkeypatch):
    monkeypatch.setattr(extract.anthropic, "Anthropic", lambda: FakeClient())
    res = client.post("/api/scan", files={"image": ("r.jpg", b"\xff\xd8", "image/jpeg")})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 3
    # Scanning alone never saves anything.
    assert client.get("/api/receipts").json() == []


def test_save_and_summarize(client):
    assert client.post("/api/receipts", json=draft_without_flag()).status_code == 201
    second = {**draft_without_flag(), "purchased_on": "2026-09-02", "items": [
        {"name": "Ground beef", "price": 6.50, "category": "Meat & Seafood"},
    ], "tax": 0, "total": 6.50}
    client.post("/api/receipts", json=second)

    s = client.get("/api/summary").json()
    assert s["receipt_count"] == 2
    assert s["items_total"] == pytest.approx(21.96)
    assert s["tax"] == pytest.approx(0.55)
    cats = {c["category"]: c for c in s["by_category"]}
    assert cats["Meat & Seafood"]["total"] == pytest.approx(14.99)
    assert cats["Snacks & Sweets"]["item_count"] == 2
    assert s["by_category"][0]["category"] == "Meat & Seafood"

    october = client.get("/api/summary", params={"start": "2026-10-01"}).json()
    assert october["receipt_count"] == 1
    assert {c["category"] for c in october["by_category"]} == {"Meat & Seafood", "Snacks & Sweets"}

    snacks = client.get("/api/items", params={"category": "Snacks & Sweets"}).json()
    assert [i["name"] for i in snacks] == ["Dark chocolate", "Tortilla chips"]


def test_recategorize_item_and_delete_receipt(client):
    rid = client.post("/api/receipts", json=draft_without_flag()).json()["id"]
    item = client.get(f"/api/receipts/{rid}").json()["items"][1]

    res = client.patch(f"/api/items/{item['id']}", json={"category": "Pantry"})
    assert res.json()["category"] == "Pantry"
    assert client.patch(f"/api/items/{item['id']}", json={"category": "Nonsense"}).status_code == 422

    assert client.delete(f"/api/receipts/{rid}").status_code == 204
    assert client.get("/api/summary").json()["items_total"] == 0
    assert client.get("/api/items").json() == []


def test_rejects_unknown_category_and_empty_receipt(client):
    bad = {**draft_without_flag(), "items": [{"name": "x", "price": 1, "category": "Gadgets"}]}
    assert client.post("/api/receipts", json=bad).status_code == 422
    assert client.post("/api/receipts", json={**draft_without_flag(), "items": []}).status_code == 422


def test_csv_export(client):
    client.post("/api/receipts", json=draft_without_flag())
    res = client.get("/api/export.csv")
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("date,store,item")
    assert len(lines) == 4
    assert "Chicken thighs" in res.text


def test_index_served(client):
    assert "Grocery Reader" in client.get("/").text


def test_corrections_are_remembered_and_applied(client, monkeypatch):
    # User fixes Claude's misreading of "MGN CHPS" while reviewing.
    receipt = {**draft_without_flag(), "items": [
        {"name": "Magnolia Chips", "raw_text": "MGN CHPS", "quantity": 2, "price": 50,
         "category": "Snacks & Sweets", "corrected": True},
        {"name": "Chicken thighs", "raw_text": "CHKN THIGH", "price": 8.49, "category": "Meat & Seafood"},
    ]}
    client.post("/api/receipts", json=receipt)
    assert client.get("/api/corrections").json() == [
        {"raw_text": "MGN CHPS", "name": "Magnolia Chips", "category": "Snacks & Sweets"}
    ]

    # Next scan: Claude misreads it again, but the saved correction wins and is in the prompt.
    misread = {**DRAFT, "items": [
        {"name": "Magnolia Chicken", "raw_text": "mgn  chps", "quantity": 2, "price": 50, "category": "Meat & Seafood"},
    ]}
    fake = FakeClient(misread)
    monkeypatch.setattr(extract.anthropic, "Anthropic", lambda: fake)
    item = client.post("/api/scan", files={"image": ("r.jpg", b"\xff\xd8", "image/jpeg")}).json()["items"][0]
    assert (item["name"], item["category"]) == ("Magnolia Chips", "Snacks & Sweets")
    prompt = fake.calls[0]["messages"][0]["content"][1]["text"]
    assert "MGN CHPS -> Magnolia Chips (Snacks & Sweets)" in prompt


def test_editing_saved_item_records_correction(client):
    rid = client.post("/api/receipts", json=draft_without_flag()).json()["id"]
    item = client.get(f"/api/receipts/{rid}").json()["items"][1]
    res = client.patch(f"/api/items/{item['id']}", json={"name": "Corn tortilla chips", "quantity": 2})
    assert res.json()["quantity"] == 2
    fixes = {c["raw_text"]: c for c in client.get("/api/corrections").json()}
    assert fixes["TORT CHIPS"]["name"] == "Corn tortilla chips"
