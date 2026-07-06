"""Pure unit tests for the id-scan schema + age helper. No network."""

from idscan.pipeline import OCR_EXTRACTION_SCHEMA, _compute_age

REQUIRED_KEYS = {
    "first_name", "last_name", "middle_name", "birth_date", "mjstateidno",
    "id_number", "id_expiration", "address", "city", "state", "postal_code",
    "phone", "email", "gender", "id_type", "accts_name",
}


def test_schema_has_required_property_keys():
    props = OCR_EXTRACTION_SCHEMA["properties"]
    for key in REQUIRED_KEYS:
        assert key in props, f"missing schema property: {key}"
    assert OCR_EXTRACTION_SCHEMA["required"] == ["first_name", "last_name"]


def test_compute_age_over_21():
    assert _compute_age("1990-01-01") >= 21


def test_compute_age_under_21():
    assert _compute_age("2010-01-01") < 21


def test_compute_age_invalid_returns_none():
    assert _compute_age(None) is None
    assert _compute_age("not-a-date") is None
