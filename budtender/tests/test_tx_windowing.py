"""get_transactions_detailed window resilience — a Dutchie 500 on one chunk must NOT abort
the backfill; halve-and-retry recovers heavy/transient windows, gives up gracefully otherwise.
Pure (no DB): _pos_get + _store are mocked."""

from datetime import datetime

from budtender import dutchie

_FROM = "2023-10-28T00:00:00+00:00"
_TO = "2023-11-28T00:00:00+00:00"


def test_subsplit_recovers_a_failing_wide_window(monkeypatch):
    monkeypatch.setattr(dutchie, "_store", lambda slug: {"pos_key": "k"})

    def fake(key, path, params):
        a = datetime.fromisoformat(params["fromDateUTC"])
        b = datetime.fromisoformat(params["toDateUTC"])
        if (b - a).days > 20:          # simulate the heavy 31-day window 500ing
            return None
        return [{"w": f"{a.date()}..{b.date()}"}]

    monkeypatch.setattr(dutchie, "_pos_get", fake)
    rows = dutchie.get_transactions_detailed("yakima", _FROM, _TO)
    assert len(rows) >= 2              # the wide window failed but the halves were recovered


def test_gives_up_gracefully_when_window_always_500s(monkeypatch):
    monkeypatch.setattr(dutchie, "_store", lambda slug: {"pos_key": "k"})
    monkeypatch.setattr(dutchie, "_pos_get", lambda *a, **k: None)   # always 500
    assert dutchie.get_transactions_detailed("yakima", _FROM, _TO) == []   # bounded, no crash
