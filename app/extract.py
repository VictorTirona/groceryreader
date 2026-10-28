"""Reads a receipt photo with Claude and returns structured line items."""

import base64
import json

import anthropic

from .categories import CATEGORIES, CATEGORY_NAMES
from .db import normalize_raw

MODEL = "claude-opus-5-5"

SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {
            "type": "boolean",
            "description": "False if the image is not a receipt or is too blurry to read.",
        },
        "store": {"type": ["string", "null"]},
        "purchased_on": {
            "type": ["string", "null"],
            "description": "Purchase date as YYYY-MM-DD, or null if not printed.",
        },
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. USD."},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "raw_text": {"type": "string"},
                    "quantity": {"type": "number"},
                    "price": {"type": "number"},
                    "category": {"type": "string", "enum": CATEGORY_NAMES},
                },
                "required": ["name", "raw_text", "quantity", "price", "category"],
                "additionalProperties": False,
            },
        },
        "tax": {"type": "number"},
        "total": {"type": ["number", "null"]},
    },
    "required": ["readable", "store", "purchased_on", "currency", "items", "tax", "total"],
    "additionalProperties": False,
}

_category_guide = "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())

PROMPT = f"""Read this grocery receipt and list every purchased item.

For each item:
- name: a plain-English product name. Expand store abbreviations when you are reasonably
  sure ("GV 2% MLK GAL" -> "Great Value 2% milk, 1 gal"); otherwise keep the printed text.
- raw_text: the product description exactly as printed, without the quantity or price
  (for "MGN CHPS x2 50.00" this is "MGN CHPS").
- quantity: the count or weight purchased (1 if not shown).
- price: the final amount paid for the line. Subtract any coupon, member discount or
  markdown printed under it, so discounts never appear as their own items.
- category: one of these:
{_category_guide}

Put sales tax in `tax` (0 if none) and the receipt's grand total in `total`. Leave out
subtotals, payment lines, change and loyalty-point lines. Bottle deposits and bag fees go
in "Other". If the image is not a receipt or cannot be read, set readable to false and
return an empty item list."""

CORRECTIONS_INTRO = """
The shopper has corrected these receipt lines before. When a line matches (or is clearly
the same product), use their name and category:
"""


class ExtractionError(Exception):
    """The receipt could not be turned into line items."""


def build_prompt(corrections: dict[str, dict]) -> str:
    if not corrections:
        return PROMPT
    lines = "\n".join(f"- {raw} -> {c['name']} ({c['category']})" for raw, c in corrections.items())
    return PROMPT + "\n" + CORRECTIONS_INTRO + lines


def apply_corrections(items: list[dict], corrections: dict[str, dict]) -> None:
    """Exact matches on the printed text always win over Claude's reading."""
    for item in items:
        fix = corrections.get(normalize_raw(item["raw_text"]))
        if fix:
            item["name"] = fix["name"]
            item["category"] = fix["category"]


def extract_receipt(
    image: bytes,
    media_type: str,
    client: anthropic.Anthropic | None = None,
    corrections: dict[str, dict] | None = None,
) -> dict:
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ExtractionError(f"Unsupported image type {media_type}. Use JPEG, PNG, GIF or WebP.")

    client = client or anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": RECEIPT_SCHEMA},
            },
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": build_prompt(corrections or {})},
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError as e:
        raise ExtractionError("Claude rejected the API key. Check ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise ExtractionError("Rate limited by the Claude API. Try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise ExtractionError(f"Claude could not process the image: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise ExtractionError(f"Claude API error ({e.status_code}). Try again.") from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError("Could not reach the Claude API.") from e

    if response.stop_reason == "refusal":
        raise ExtractionError("Claude declined to read this image.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("The receipt was too long to read in one pass.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ExtractionError("Claude returned no result.")
    receipt = json.loads(text)
    if not receipt["readable"]:
        raise ExtractionError("That doesn't look like a readable receipt. Try a sharper, flatter photo.")
    del receipt["readable"]
    apply_corrections(receipt["items"], corrections or {})
    return receipt
