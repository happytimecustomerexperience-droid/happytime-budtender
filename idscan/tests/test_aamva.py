"""Local id-scan: AAMVA field parse + a real PDF417 encode->decode->parse round-trip.
Fully offline — proves the OCR path needs no cloud."""
import io
import warnings

from idscan.aamva import parse_aamva
from idscan.pipeline import run_id_scan

# A realistic AAMVA DL payload (US, MMDDCCYY dates); elements are \n-separated.
SAMPLE = (
    "@\n\x1e\rANSI 636000090002DL00410288ZV03290015DL\n"
    "DCSDOE\n"
    "DACJOHN\n"
    "DADQUINCY\n"
    "DBB01151990\n"
    "DBA01152030\n"
    "DAQD12345678\n"
    "DAG123 MAIN ST\n"
    "DAHAPT 4\n"
    "DAISPOKANE\n"
    "DAJWA\n"
    "DAK992010000\n"
    "DBC1\n"
)


def test_parse_aamva_fields():
    f = parse_aamva(SAMPLE)
    assert f["first_name"] == "JOHN"
    assert f["last_name"] == "DOE"
    assert f["middle_name"] == "QUINCY"
    assert f["birth_date"] == "1990-01-15"
    assert f["id_expiration"] == "2030-01-15"
    assert f["id_number"] == "D12345678"
    assert f["mjstateidno"] is None
    assert f["address2"] == "APT 4"
    assert f["state"] == "WA"
    assert f["city"] == "SPOKANE"
    assert f["postal_code"] == "99201-0000"
    assert f["gender"] == "male"
    assert f["accts_name"] == "JOHN DOE"


def test_parse_aamva_rejects_junk():
    assert parse_aamva("") is None
    assert parse_aamva("not a barcode at all") is None


def _pdf417_png(text: str) -> bytes:
    import zxingcpp
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.PDF417, text)  # zxingcpp.Image (grayscale buffer)
    h, w = img.shape[0], img.shape[1]
    pil = Image.frombuffer("L", (w, h), bytes(img), "raw", "L", 0, 1)
    pil = pil.resize((w * 4, h * 4), Image.NEAREST)  # upscale 1px modules so it decodes robustly
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def test_run_id_scan_local_barcode_roundtrip():
    out = run_id_scan([_pdf417_png(SAMPLE)])
    assert "error" not in out, out
    assert out["first_name"] == "JOHN" and out["last_name"] == "DOE"
    assert out["birth_date"] == "1990-01-15"
    assert out["over_21"] is True
    assert out["age"] and out["age"] >= 21
    assert out["id_type"] == "driver_license"


def test_run_id_scan_no_barcode_no_keys_errors(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_AI_KEY", raising=False)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(buf, "PNG")
    out = run_id_scan([buf.getvalue()])
    assert "error" in out          # no barcode + no cloud keys -> graceful error, no network


def test_an_expired_card_is_flagged_expired():
    """WAC 314-55-150(2) — an expired document cannot verify age, at any age."""
    from idscan.pipeline import _is_expired
    assert _is_expired("2000-01-01") is True


def test_a_card_expiring_today_is_still_valid_today():
    from datetime import date
    from idscan.pipeline import _is_expired, _store_today
    assert _is_expired(_store_today().isoformat()) is False
    assert isinstance(_store_today(), date)


def test_an_unreadable_expiry_is_unknown_not_valid():
    # None must never read as "fine" — the caller sends it to a manual check.
    from idscan.pipeline import _is_expired
    for bad in (None, "", "not-a-date", "0000-00-00"):
        assert _is_expired(bad) is None


def test_age_uses_store_local_time_not_utc():
    # A UTC comparison flips a birthday up to 8h early; on a 21st birthday that is
    # the difference between a lawful sale and a gross misdemeanour.
    from idscan.pipeline import _store_today
    import datetime as dt
    assert abs((_store_today() - dt.datetime.utcnow().date()).days) <= 1
