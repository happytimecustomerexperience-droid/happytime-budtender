"""Shared PDF-ingest logic for PolicyDocument (22-SPEC-kb-seed.md §3.2) — so a future
return-policy / privacy / loyalty PDF can be parsed into the spoken-grounding ``body``,
not just stored as bytes.

Ported from swedish-bot/kb/ingest.py (``parse_pdf_text``~L24, ``ingest_pdf_bytes``~L39,
``MAX_PDF_BYTES``~L15 — the staff-gated-but-fail-closed bounded-PDF idiom), adapted to the
voice PolicyDocument (``body``/``sha256``/``pdf`` instead of Swedish ``parsed_text``).

Idempotent: the parsed text lands in ``PolicyDocument.body`` and the bytes' sha256 in
``.sha256`` so the semantic content-hash cache self-invalidates when a doc is replaced.
"""

from __future__ import annotations

import hashlib
import io

# Staff-gated, but still fail-closed: only real PDFs, bounded size + pages.
MAX_PDF_BYTES = 40 * 1024 * 1024  # 40 MB — policy PDFs are small but not unbounded
MAX_PDF_PAGES = 2000  # cap parse work so a forged PDF can't pin CPU
_PDF_MAGIC = b"%PDF-"


class IngestError(ValueError):
    """Raised on a non-PDF / oversized upload (surfaced to the staff UI)."""


def parse_pdf_text(source, *, max_pages: int = MAX_PDF_PAGES) -> str:
    """source: a filesystem path str or a binary file-like object. Returns "" if pdfplumber
    is unavailable (degrade-safe — the row still seeds from its authored body)."""
    try:
        import pdfplumber
    except ImportError:
        return ""
    out = []
    with pdfplumber.open(source) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            out.append(page.extract_text() or "")
    return "\n".join(out)


def ingest_pdf_bytes(
    policy,
    data: bytes,
    filename: str,
    *,
    kind: str = "return_policy",
    title: str | None = None,
    citation: str = "",
    replace=None,
):
    """Validate + parse + persist a policy PDF. The parsed text becomes the spoken-grounding
    ``body``; the bytes' sha256 lands on ``.sha256`` so the cache key self-invalidates. If
    ``replace`` is given, overwrite that document in place; else use the passed ``policy``.
    Callers should size-check the upload BEFORE materializing ``data``.

    Returns the saved PolicyDocument. Idempotent on the ``kind`` natural key (unique)."""
    from django.core.files.base import ContentFile

    if not data or data[:5] != _PDF_MAGIC:
        raise IngestError("Not a PDF file (missing %PDF- header).")
    if len(data) > MAX_PDF_BYTES:
        raise IngestError(
            f"PDF too large ({len(data) // 1024 // 1024} MB > {MAX_PDF_BYTES // 1024 // 1024} MB)."
        )

    text = parse_pdf_text(io.BytesIO(data))
    doc = replace or policy
    doc.kind = kind
    if title:
        doc.title = title
    if citation:
        doc.citation = citation
    if text:  # keep an authored body if the PDF has no extractable text layer
        doc.body = text
    doc.sha256 = hashlib.sha256(data).hexdigest()
    old_name = replace.pdf.name if (replace and replace.pdf) else None
    doc.pdf.save(filename, ContentFile(data), save=True)
    # Replace-in-place: remove the superseded physical file (FileField.save doesn't).
    if old_name and old_name != doc.pdf.name:
        try:
            doc.pdf.storage.delete(old_name)
        except Exception:  # noqa: BLE001 — cleanup is best-effort; never break the upload
            pass
    return doc
