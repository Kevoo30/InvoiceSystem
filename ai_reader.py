"""AI reader: turns a photo or PDF of a delivery note / invoice into form values.

The AI only READS the document. It never decides whether to pay: the numbers it
returns fill the form, a person checks them, and matching.py does the checking.

Needs ANTHROPIC_API_KEY in .env. Without it the screens still work by typing.
"""
import base64
import io
import json
import os
import re
from datetime import datetime
from difflib import get_close_matches

from sqlalchemy import select

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_SIDE = 1800          # pixels; phone photos are shrunk before sending

PROMPT = """You read a {kind_name} from a Kenyan building-materials supplier.
Return ONLY a JSON object, no other text:
{{"number": "<document number or null>",
  "date": "<YYYY-MM-DD or null>",
  "lines": [{{"name": "<item text as written>", "qty": <number>,
              "returned": <number>, "price": <unit price number or null>}}]}}
Rules:
- One entry per item line. Copy item names as written.
- qty is the quantity on that line. Use plain numbers, no commas.
- price is the price for ONE unit (not the line total). null if not shown.
- returned: quantity marked as returned, rejected or sent back (for example
  "X RETURNED" or "RETURNED" beside a line means the whole qty). 0 if none.
- Ignore totals, VAT, delivery charges and signatures.
- If a value is unreadable use null. Never guess."""

KIND_NAME = {"delivery": "delivery note", "invoice": "invoice"}


class ReaderError(Exception):
    """Message is safe to show to the user."""


# ---------------------------------------------------------------- names
def normalise(text: str) -> str:
    """HOOP IRON -> 'hoop iron'; 'Cutting Orient Craft 9"' -> 'cutting orient craft 9'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def map_lines(session, supplier_id: int, lines: list[dict], material_ids: dict):
    """Match item names to this supplier's materials.

    material_ids: {material_id: material_name} for the supplier.
    Order of trying: supplier alias, exact name, close spelling.
    Returns (mapped, unmatched) where mapped = {material_id: {qty, returned, price}}.
    """
    from models import SupplierAlias
    aliases = {a.alias: a.material_id for a in session.scalars(
        select(SupplierAlias).where(SupplierAlias.supplier_id == supplier_id))}
    by_name = {normalise(n): mid for mid, n in material_ids.items()}

    mapped, unmatched = {}, []
    for ln in lines:
        raw = str(ln.get("name") or "").strip()
        key = normalise(raw)
        mid = aliases.get(key) or by_name.get(key)
        if mid is None and key:
            close = get_close_matches(key, list(by_name) + list(aliases), n=1, cutoff=0.82)
            if close:
                mid = by_name.get(close[0]) or aliases.get(close[0])
        if mid is None or mid not in material_ids:
            unmatched.append(raw or "(blank)")
            continue
        slot = mapped.setdefault(mid, {"qty": 0.0, "returned": 0.0, "price": None})
        slot["qty"] += _num(ln.get("qty")) or 0.0
        slot["returned"] += _num(ln.get("returned")) or 0.0
        price = _num(ln.get("price"))
        if price and slot["price"] is None:
            slot["price"] = price
    return mapped, unmatched


def _num(v):
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


# ---------------------------------------------------------------- reading
def _shrink(data: bytes, media_type: str):
    """Shrink big phone photos so the upload stays small. Skips PDFs."""
    if media_type == "application/pdf":
        return data, media_type
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, media_type


def _parse(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ReaderError("The photo could not be read. Try a clearer photo.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ReaderError("The photo could not be read. Try a clearer photo.")


def read_document(data: bytes, media_type: str, kind: str, client=None) -> dict:
    """Send the file to Claude and return {'number', 'date', 'lines': [...]}."""
    if kind not in KIND_NAME:
        raise ReaderError("Unknown document type.")
    if client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ReaderError("Photo reading is not set up. Add ANTHROPIC_API_KEY to .env, "
                              "or type the values in.")
        try:
            import anthropic
        except ImportError:
            raise ReaderError("Photo reading needs the anthropic package: pip install anthropic")
        client = anthropic.Anthropic(api_key=key)

    data, media_type = _shrink(data, media_type)
    block_type = "document" if media_type == "application/pdf" else "image"
    content = [
        {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": PROMPT.format(kind_name=KIND_NAME[kind])},
    ]
    try:
        reply = client.messages.create(model=MODEL, max_tokens=2000,
                                       messages=[{"role": "user", "content": content}])
    except Exception:
        raise ReaderError("Photo reading failed. Try again or type the values in.")
    text = "".join(getattr(b, "text", "") for b in reply.content)
    out = _parse(text)
    lines = out.get("lines")
    out["lines"] = lines if isinstance(lines, list) else []
    out["number"] = (str(out["number"]).strip()[:50] if out.get("number") else None)
    try:
        out["date"] = datetime.strptime(str(out.get("date")), "%Y-%m-%d").date().isoformat()
    except ValueError:
        out["date"] = None
    return out
