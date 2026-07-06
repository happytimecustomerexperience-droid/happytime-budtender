# ponytail: id_check OCR core, vendored standalone. DB writes (customer_profile
# INSERT, tenant_schema_cursor, Dutchie update-customer, new_customer audit) removed —
# that work is handled elsewhere now. This module is OCR -> LLM extraction ONLY.
"""ID-scan pipeline: Mistral OCR -> OpenAI structured extraction -> field dict."""

import base64
import json
import logging
import os
from datetime import date, datetime

import requests

logger = logging.getLogger(__name__)

# ---------- OCR -> structured extraction schema ----------

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


def _ocr_with_mistral(image_bytes_list: list[bytes], mistral_api_key: str) -> str:
    """Call Mistral OCR for each image and return merged OCR text."""
    texts = []
    for i, img_bytes in enumerate(image_bytes_list):
        b64 = base64.b64encode(img_bytes).decode("ascii")
        mime = "image/jpeg"
        try:
            resp = requests.post(
                "https://api.mistral.ai/v1/ocr",
                headers={
                    "Authorization": f"Bearer {mistral_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MISTRAL_OCR_MODEL,
                    "document": {
                        "type": "image_url",
                        "image_url": f"data:{mime};base64,{b64}",
                    },
                },
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
    user_msg = f"Extract customer information.\n\nOCR Text:\n{ocr_text}"
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_EXTRACTION_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "id_extraction",
                    "strict": True,
                    "schema": OCR_EXTRACTION_SCHEMA,
                },
            },
            "temperature": 0.1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


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


# ---------- Public entry point ----------

def run_id_scan(image_bytes_list: list[bytes]) -> dict:
    """Run OCR + structured extraction on ID images.

    A single image is duplicated for both sides (n8n parity). On any external
    failure returns {"error": "<reason>"} (never raises) so callers degrade.
    Keys from os.environ: MISTRAL_API_KEY, OPEN_AI_KEY.
    """
    if not image_bytes_list:
        return {"error": "No images provided"}

    images = list(image_bytes_list)
    if len(images) == 1:
        images.append(images[0])

    mistral_key = os.environ.get("MISTRAL_API_KEY")
    openai_key = os.environ.get("OPEN_AI_KEY")
    if not mistral_key or not openai_key:
        return {"error": "Server missing OCR/AI credentials"}

    # 1. OCR
    ocr_text = _ocr_with_mistral(images, mistral_key)
    if not ocr_text or ("(no text extracted)" in ocr_text and "(OCR failed" in ocr_text):
        return {"error": "OCR failed to extract text from images"}

    # 2. Structured extraction
    try:
        extracted = _extract_with_openai(ocr_text, openai_key)
    except Exception as e:
        logger.exception("OpenAI extraction failed: %s", e)
        return {"error": f"LLM extraction failed: {e}"}

    # Build accts_name if missing
    if not extracted.get("accts_name"):
        extracted["accts_name"] = " ".join(filter(None, [
            extracted.get("first_name", ""),
            extracted.get("last_name", ""),
        ]))

    birth_date = extracted.get("birth_date")
    if birth_date and not _is_valid_date(birth_date):
        birth_date = None
    age = _compute_age(birth_date)
    over_21 = age is not None and age >= 21

    return {
        "first_name": extracted.get("first_name"),
        "last_name": extracted.get("last_name"),
        "middle_name": extracted.get("middle_name"),
        "accts_name": extracted.get("accts_name"),
        "birth_date": birth_date,
        "age": age,
        "over_21": over_21,
        "mjstateidno": extracted.get("mjstateidno"),
        "id_number": extracted.get("id_number"),
        "address": extracted.get("address"),
        "city": extracted.get("city"),
        "state": extracted.get("state"),
        "postal_code": extracted.get("postal_code"),
        "phone": extracted.get("phone"),
        "email": extracted.get("email"),
        "gender": extracted.get("gender"),
        "id_type": extracted.get("id_type"),
        "ocr_text": ocr_text,
    }
