"""A date of birth a model read off the card never auto-passes the 21+ gate.

Only the barcode's DOB may. On the optional OCR+LLM fallback (front-only cards) the
scan comes back over_21 None — the POS's "CHECK BY HAND" state — so staff read the DOB
printed on the card. Fully offline: the Mistral/OpenAI calls are stubbed.
"""
import pytest

from idscan import pipeline
from idscan.tests.test_aamva import SAMPLE


def _ocr_scan(monkeypatch, extracted: dict) -> dict:
    monkeypatch.setenv("MISTRAL_API_KEY", "m")
    monkeypatch.setenv("OPEN_AI_KEY", "o")
    monkeypatch.setattr(pipeline, "_decode_barcode", lambda img: None)       # front of the card: no barcode
    monkeypatch.setattr(pipeline, "_ocr_with_mistral", lambda imgs, key: "Image 1:\nJANE DOE DOB 01/15/1990")
    monkeypatch.setattr(pipeline, "_extract_with_openai", lambda text, key: dict(extracted))
    return pipeline.run_id_scan([b"front-of-card"])


BASE = {"first_name": "Jane", "last_name": "Doe", "id_expiration": "2099-01-01"}


def test_a_model_extracted_21_plus_dob_is_not_an_automatic_pass(monkeypatch):
    out = _ocr_scan(monkeypatch, {**BASE, "birth_date": "1990-01-15"})

    assert "error" not in out, out
    assert out["age"] >= 21                      # staff still see the DOB and age the model read
    assert out["birth_date"] == "1990-01-15"
    assert out["over_21"] is None                # ... but it is not True: the POS says CHECK BY HAND


def test_a_model_extracted_under_21_dob_is_still_a_refusal(monkeypatch):
    out = _ocr_scan(monkeypatch, {**BASE, "birth_date": "2015-06-01"})

    assert out["over_21"] is False               # failing the gate stays automatic


def test_a_model_extraction_with_no_dob_does_not_pass(monkeypatch):
    out = _ocr_scan(monkeypatch, {**BASE, "birth_date": None})

    assert out["over_21"] is not True


@pytest.mark.parametrize("scan", [
    lambda: pipeline.run_id_scan_payload(SAMPLE),                              # client-side barcode scan
    lambda: pipeline.run_id_scan([b"back-of-card"]),                           # server-side barcode decode
])
def test_a_barcode_dob_still_passes(monkeypatch, scan):
    monkeypatch.setattr(pipeline, "_decode_barcode", lambda img: SAMPLE)

    out = scan()

    assert out["birth_date"] == "1990-01-15"
    assert out["over_21"] is True


def test_the_barcode_wins_over_the_ocr_fallback(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "m")
    monkeypatch.setenv("OPEN_AI_KEY", "o")
    monkeypatch.setattr(pipeline, "_decode_barcode", lambda img: SAMPLE)

    def boom(*a, **k):
        raise AssertionError("OCR must not run when the barcode decoded")

    monkeypatch.setattr(pipeline, "_ocr_with_mistral", boom)

    assert pipeline.run_id_scan([b"back-of-card"])["over_21"] is True
