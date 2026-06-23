"""Call summary via core/services/gemini.py (10-P0-CHASSIS-FAQ.md §3.3).

Called INLINE by the eocr handler in P0 (moved to Celery in P5, gated). Degrade-safe: with no
Gemini auth / on any error it returns ``""`` so the durable ``VoiceCall`` write is never blocked
(ADR-017 — the record is never lost). Numbers-Guard holds: the model phrases the transcript, it
never originates a figure.
"""

from __future__ import annotations

import logging

from core import constants
from core.services import gemini

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You summarize a cannabis-retail phone call in 1-2 short sentences for store staff. "
    "State the caller's intent and the outcome. Do not invent details not in the transcript. "
    "Do not state any price, cost, or margin figure."
)


def summarize_call(voice_call) -> str:
    """Return a short staff-facing summary of the call, or ``""`` if unavailable.

    Reads ``voice_call.transcript``; never raises (a summary failure must not lose the record)."""
    transcript = (getattr(voice_call, "transcript", "") or "").strip()
    if not transcript:
        return ""
    try:
        resp = gemini.generate(
            f"Transcript:\n{transcript}",
            model=constants.MODELS["flash"],
            system_instruction=_SYSTEM,
            max_output_tokens=120,
            temperature=0.2,
        )
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001 — a summary failure must not block the durable write
        logger.warning("summarize_call failed; continuing without summary", exc_info=True)
        return ""
