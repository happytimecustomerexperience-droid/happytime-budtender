"""Dutchie reports "nobody has this number" as an ERROR, and it is not one.

Captured live 2026-08-10: a 10-digit phone with no account comes back

    HTTP 200  {"Result": false, "Message": "No matching guests found"}

not an empty Data list. `PosClient.post` turns every Result=false into
DutchieUnavailable — correct for the rest of the API, wrong for a search — so a
genuine "no account" was indistinguishable from a register outage at the source,
for every caller. It surfaced the day /loyalty had to tell someone their number
isn't registered and could only manage "we couldn't check right now".

The narrowness is the whole design. The same endpoint refuses a too-broad query
with "Please Narrow Your Search", which means the search never ran — reporting that
as "no account" would be a confident wrong answer.
"""
import pytest

from dutchie.pos_register_client import PosRegisterClient
from dutchie.session import DutchieUnavailable, Store

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=8002, lsp_id=1745, loc_id=3498, register_id=8318,
              username="u", password="p")


def client(monkeypatch, raises=None, returns=None):
    c = PosRegisterClient(STORE)

    def fake_post(path, body, **kw):
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(c, "post", fake_post)
    monkeypatch.setattr(c, "session_block", lambda **kw: {})
    return c


def test_no_matching_guests_is_an_empty_result_not_an_error(monkeypatch):
    """THE FIX. Verbatim message from the live capture."""
    c = client(monkeypatch, raises=DutchieUnavailable(
        "https://ash.pos.dutchie.com/api/v2/guest/checkin_search_by_string "
        "Result=false: 'No matching guests found'"))
    assert c.guest_search("5095550177") == {"Result": True, "Data": []}


def test_a_search_too_broad_to_run_still_raises(monkeypatch):
    """"Please Narrow Your Search" means Dutchie never looked. Answering "no
    account" to a search that did not happen is worse than admitting we don't know."""
    c = client(monkeypatch, raises=DutchieUnavailable(
        "checkin_search_by_string Result=false: 'Please Narrow Your Search'"))
    with pytest.raises(DutchieUnavailable):
        c.guest_search("509")


def test_a_real_outage_still_raises(monkeypatch):
    c = client(monkeypatch, raises=DutchieUnavailable("POST ...: ConnectionError"))
    with pytest.raises(DutchieUnavailable):
        c.guest_search("5095551212")


def test_a_match_is_returned_untouched(monkeypatch):
    rows = {"Result": True, "Data": [{"Guest_id": 1, "Name": "Sam", "PhoneNo": "5095551212"}]}
    assert client(monkeypatch, returns=rows).guest_search("5095551212") == rows


def test_the_match_is_case_insensitive_on_dutchies_wording(monkeypatch):
    # Dutchie's casing is not a contract; the message text is what we key on, so
    # match it the way a human reads it rather than byte-for-byte.
    for wording in ("No matching guests found", "no matching guests FOUND",
                    "Result=false: 'No Matching Guests Found'"):
        c = client(monkeypatch, raises=DutchieUnavailable(wording))
        assert c.guest_search("5095550177")["Data"] == []
