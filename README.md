# Grocery Reader

Snap a photo of a grocery receipt and every line item lands in a spending tracker, sorted into a
category (meat, snacks, produce, …), so you can see where your grocery money goes.

## How a receipt is read

There is no separate OCR step. The photo goes straight to Claude (a vision-capable AI model),
which reads the printed text, expands store abbreviations into product names, and picks a
category for each line in one pass. The answer comes back as structured JSON that matches a fixed
schema (`app/extract.py`), so the app always gets the same fields: name, printed text, quantity,
price and category.

Nothing is saved until you have checked it. The review screen shows each item next to what the
receipt actually says, and you can change the name, quantity, price or category, add a missed
line, or remove one.

### Corrections are remembered

If Claude reads `MGN CHPS` as "Magnolia Chicken" and you change it to "Magnolia Chips", that fix
is stored against the printed text `MGN CHPS`. On every later scan:

1. Your past corrections are given to Claude as hints, so similar lines are read the same way.
2. Any line whose printed text matches a correction exactly is overwritten with your name and
   category, whatever Claude guessed.

Edits you make later in the **Receipts** tab are remembered the same way.

## Running it

Requires Python 3.10+ and an Anthropic API key (get one at https://console.anthropic.com/).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. To use your phone's camera, open `http://<your-computer's-LAN-IP>:8000`
from your phone on the same Wi-Fi. Some phone browsers only allow the camera on HTTPS pages; if
**Take photo** won't open the camera, use **Upload image** and choose a photo you've already taken.

Data lives in `data/groceries.db` (SQLite). Set `GROCERY_DB_PATH` to put it somewhere else.

## Using it

- **Scan**: take or upload a photo, check the items (the app warns if items + tax don't add
  up to the printed total), then save.
- **Spending**: totals per category for a chosen period. Tap a category to see its items.
  **Export CSV** downloads every item for use in a spreadsheet.
- **Receipts**: every saved receipt. Edit items in place (changes save automatically) or delete
  a receipt.

Categories are defined in `app/categories.py`. Edit that file to add or rename one.

## Cost

Each scan is one Claude API call with one image. Expect a few US cents per receipt, a bit more for
very long ones. The model is set in `app/extract.py`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests replace the Claude call with a fake, so they run offline and free.
