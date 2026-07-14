# ponytail: id_check core, vendored standalone. OCR is now LOCAL by default — the
# PDF417/AAMVA barcode on the back of the ID is decoded in-process (zxing-cpp), no
# network. The old cloud OCR+LLM path is kept ONLY as an optional fallback for
# front-only cards, and only when MISTRAL_API_KEY + OPEN_AI_KEY are configured.
"""ID-scan pipeline: local PDF417/AAMVA decode (-> optional cloud OCR fallback) -> field dict."""

import base64
import io
import json
import logging
import os
from datetime import date, datetime

import requests

from .aamva import parse_aamva

logger = logging.getLogger(__name__)

# ---------- structured extraction schema (used by the optional cloud fallback) ----------

OCR_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "first_name": {"type": "string", "description": "First name"},
        "last_name": {"type": "string", "description": "Last name"},
        "middle_name": {"type": ["string", "null"], "description": "Middle name"},
        "birth_date": {"type": ["string", "null"], "description": "Birth date YYYY-MM-DD"},
        "mjstateidno": {"type": ["string", "null"], "description": "State or military ID number, preserve leading zeros"},
        "id_number": {"type": ["string", "null"], "description": "ID number if different"},
        "id_expiration": {"type": ["string", "null"], "description": "ID expiration"},
        "state": {"type": ["string", "null"], "description": "State"},
        "address": {"type": ["string", "null"], "description": "Address line 1"},
        "address2": {"type": ["string", "null"], "description": "Address line 2"},
        "city": {"type": ["string", "null"], "description": "City"},
        "postal_code": {"type": ["string", "null"], "description": "Postal code"},
        "phone": {"type": ["string", "null"], "description": "Phone"},
        "email": {"type": ["string", "null"], "description": "Email"},
        "gender": {"type": ["string", "null"], "description": "Gender"},
        "id_type": {
            "type": "string",
            "enum": ["driver_license", "military_id", "other"],
            "description": "Type of ID",
        },
        "accts_name": {"type": "string", "description": "Full name for account (FirstName LastName)"},
    },
    "required": ["first_name", "last_name"],
}

SYSTEM_PROMPT = (
    "You are an ID document data extractor. Given OCR text from a driver license or military ID, "
    "extract structured customer information. Return valid JSON matching the required schema. "
    "Preserve leading zeros in ID numbers. Format birth_date as YYYY-MM-DD. "
    "For accts_name, combine first_name and last_name (and middle_name if present)."
)

MISTRAL_OCR_MODEL = "mistral-ocr-latest"
OPENAI_EXTRACTION_MODEL = "gpt-4.1-mini"


# ---------- LOCAL: PDF417/AAMVA barcode decode ----------

def _decode_barcode(image_bytes: bytes) -> str | None:
    """Decode a PDF417 (or any) barcode from one image, LOCALLY. Returns the raw
    payload text, or None. Never raises."""
    try:
        import zxingcpp
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        for r in zxingcpp.read_barcodes(img):
            # AAMVA payloads carry raw control chars (\n, \x1e, \r); .text escapes
            # them as literal "<LF>" etc., so prefer .bytes for the raw payload.
            raw = getattr(r, "bytes", None)
            if raw:
                return bytes(raw).decode("utf-8", "replace")
            if getattr(r, "text", None):
                return r.text
    except Exception as e:  # noqa: BLE001 — best-effort, fall through to the fallback
        logger.debug("local barcode decode failed: %s", e)
    return None


# ---------- Optional cloud fallback (only if keys are configured) ----------

def _ocr_with_mistral(image_bytes_list: list[bytes], mistral_api_key: str) -> str:
    """Call Mistral OCR for each image and return merged OCR text."""
    texts = []
    for i, img_bytes in enumerate(image_bytes_list):
        b64 = base64.b64encode(img_bytes).decode("ascii")
        try:
            resp = requests.post(
                "https://api.mistral.ai/v1/ocr",
                headers={"Authorization": f"Bearer {mistral_api_key}", "Content-Type": "application/json"},
                json={"model": MISTRAL_OCR_MODEL,
                      "document": {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64}"}},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            pages = data.get("pages") or []
            raw = (pages[0].get("markdown", "") if pages else "") or data.get("text", "")
            texts.append(f"Image {i + 1}:\n{raw.strip() or '(no text extracted)'}")
        except Exception as e:
            logger.warning("Mistral OCR failed for image %d: %s", i + 1, e)
            texts.append(f"Image {i + 1}:\n(OCR failed: {e})")
    return "\n\n".join(texts)


def _extract_with_openai(ocr_text: str, openai_api_key: str) -> dict:
    """Call OpenAI to extract structured fields from OCR text."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {openai_api_key}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_EXTRACTION_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Extract customer information.\n\nOCR Text:\n{ocr_text}"},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "id_extraction", "strict": True, "schema": OCR_EXTRACTION_SCHEMA}},
            "temperature": 0.1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


# ---------- helpers ----------

def _compute_age(birth_date_str: str | None) -> int | None:
    if not birth_date_str:
        return None
    try:
        dob = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _is_valid_date(s) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _finalize(fields: dict, source_text: str) -> dict:
    """Add accts_name/age/over_21 and normalize into the public field dict."""
    if not fields.get("accts_name"):
        fields["accts_name"] = " ".join(filter(None, [fields.get("first_name", ""), fields.get("last_name", "")]))
    birth_date = fields.get("birth_date")
    if birth_date and not _is_valid_date(birth_date):
        birth_date = None
    age = _compute_age(birth_date)
    return {
        "first_name": fields.get("first_name"),
        "last_name": fields.get("last_name"),
        "middle_name": fields.get("middle_name"),
        "accts_name": fields.get("accts_name"),
        "birth_date": birth_date,
        "age": age,
        "over_21": age is not None and age >= 21,
        "mjstateidno": fields.get("mjstateidno"),
        "id_number": fields.get("id_number"),
        "id_expiration": fields.get("id_expiration"),
        "address": fields.get("address"),
        "address2": fields.get("address2"),
        "city": fields.get("city"),
        "state": fields.get("state"),
        "postal_code": fields.get("postal_code"),
        "phone": fields.get("phone"),
        "email": fields.get("email"),
        "gender": fields.get("gender"),
        "id_type": fields.get("id_type"),
        "ocr_text": source_text,
    }


def run_id_scan_payload(payload: str | None) -> dict:
    """Parse a raw ID payload provided by a client-side scan."""
    if not payload or not isinstance(payload, str):
        return {"error": "No payload provided"}
    fields = parse_aamva(payload.strip())
    if fields and (fields.get("first_name") or fields.get("last_name")):
        return _finalize(fields, source_text=payload.strip())
    return {"error": "Couldn't parse ID payload"}


# ---------- Public entry point ----------

def run_id_scan(image_bytes_list: list[bytes]) -> dict:
    """Run LOCAL barcode decode (then optional cloud OCR fallback) on ID images.

    Primary path is fully local: the PDF417/AAMVA barcode on the BACK of the card.
    If no barcode is readable AND MISTRAL_API_KEY + OPEN_AI_KEY are set, falls back
    to the cloud OCR+LLM path. On any failure returns {"error": ...} (never raises).
    """
    if not image_bytes_list:
        return {"error": "No images provided"}
    images = list(image_bytes_list)

    # 1. LOCAL — PDF417/AAMVA barcode (reliable, no network).
    for img in images:
        payload = _decode_barcode(img)
        if payload:
            fields = parse_aamva(payload)
            if fields and (fields.get("first_name") or fields.get("last_name")):
                return _finalize(fields, source_text=payload)

    # 2. Optional cloud fallback (front-only cards) — only if keys are configured.
    mistral_key = os.environ.get("MISTRAL_API_KEY")
    openai_key = os.environ.get("OPEN_AI_KEY")
    if mistral_key and openai_key:
        if len(images) == 1:
            images.append(images[0])
        ocr_text = _ocr_with_mistral(images, mistral_key)
        if ocr_text and not ("(no text extracted)" in ocr_text and "(OCR failed" in ocr_text):
            try:
                extracted = _extract_with_openai(ocr_text, openai_key)
                return _finalize(extracted, source_text=ocr_text)
            except Exception as e:
                logger.exception("OpenAI extraction failed: %s", e)
                return {"error": f"LLM extraction failed: {e}"}

    return {"error": "Couldn't read the ID — scan the BACK of the card (the barcode side)."}
