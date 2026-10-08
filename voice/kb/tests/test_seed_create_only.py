"""seed_kb is CREATE-ONLY by default (the boot-time seed must never reset owner edits).

The root docker-compose runs ``seed_kb`` at every voice-web start. It used to ``update_or_create``
every row, which reset dashboard edits to agent prompts/model/voice/greeting and the seeded store
facts (and, with publish-on-save on, pushed the defaults to Vapi). Now a missing row is inserted and
an existing row is kept; ``seed_kb --refresh`` restores the code defaults on purpose. Offline.
"""

from __future__ import annotations

import inspect
import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.forms.models import model_to_dict

from kb import models as m
from kb import seed

_SEEDED_MODELS = (
    m.FAQEntry,
    m.PolicyCategory,
    m.PolicyDocument,
    m.StoreFact,
    m.WeightTypeTaxonomy,
    m.EducationDoc,
    m.BlogDoc,
    m.AgentPrompt,
)


def _snapshot() -> dict[str, list[dict]]:
    """Every seeded row's fields (minus auto timestamps), per model, in a stable order."""
    out = {}
    for model in _SEEDED_MODELS:
        rows = []
        for obj in model.objects.order_by("pk"):
            d = model_to_dict(obj)
            d.pop("updated_at", None)
            rows.append(d)
        out[model.__name__] = rows
    return out


def _edit_owner_rows():
    """What an owner does on the dashboard: edit the greeter's prompt, model and greeting, and the
    Yakima hours row."""
    p = m.AgentPrompt.objects.get(role="entry_router")
    p.body = "OWNER PROMPT"
    p.vapi_model = "gemini-2.5-flash-lite"
    p.first_message = "Hi, Happy Time here!"
    p.save()
    h = m.StoreFact.objects.get(store="yakima", kind="hours", label="Yakima hours")
    h.value = "9 AM–9 PM daily (owner edit)"
    h.save()


def _assert_owner_rows_kept():
    p = m.AgentPrompt.objects.get(role="entry_router")
    assert p.body == "OWNER PROMPT"
    assert p.vapi_model == "gemini-2.5-flash-lite"
    assert p.first_message == "Hi, Happy Time here!"
    h = m.StoreFact.objects.get(store="yakima", kind="hours", label="Yakima hours")
    assert h.value == "9 AM–9 PM daily (owner edit)"


@pytest.mark.django_db
def test_owner_edits_survive_a_second_seed():
    seed.seed_all()
    _edit_owner_rows()
    seed.seed_all()  # the next voice-web restart
    _assert_owner_rows_kept()
    assert seed.LAST_RUN["created"] == 0 and seed.LAST_RUN["refreshed"] == 0


@pytest.mark.django_db
def test_owner_edits_survive_the_seed_kb_command():
    call_command("seed_kb", stdout=StringIO())
    _edit_owner_rows()
    out = StringIO()
    call_command("seed_kb", stdout=out)  # exactly what docker-compose runs on start
    _assert_owner_rows_kept()
    assert "create-only" in out.getvalue() and "created 0," in out.getvalue()


@pytest.mark.django_db
def test_refresh_restores_the_code_defaults():
    seed.seed_all()
    defaults = _snapshot()
    _edit_owner_rows()
    out = StringIO()
    call_command("seed_kb", "--refresh", stdout=out)
    p = m.AgentPrompt.objects.get(role="entry_router")
    assert p.body.startswith(seed.ENTRY_ROUTER_BODY)
    assert p.vapi_model == seed.VAPI_MODEL
    assert p.first_message == seed.ENTRY_FIRST_MESSAGE
    h = m.StoreFact.objects.get(store="yakima", kind="hours", label="Yakima hours")
    assert h.value == "8 AM–11:30 PM daily (open late)"
    assert _snapshot() == defaults
    assert "--refresh" in out.getvalue() and seed.LAST_RUN["created"] == 0
    # --overwrite is the same switch.
    _edit_owner_rows()
    call_command("seed_kb", "--overwrite", stdout=StringIO())
    assert m.AgentPrompt.objects.get(role="entry_router").body.startswith(seed.ENTRY_ROUTER_BODY)


@pytest.mark.django_db
def test_fresh_db_seeds_exactly_what_the_overwrite_seed_did():
    """On an empty DB create-only writes the same rows, with the same values and the same per-block
    counts, as the old update_or_create seed (which is what refresh mode still runs)."""
    counts = seed.seed_all()
    assert counts == {
        "faq": 18,
        "site_faq": 42,
        "site_education": 10,
        "return_policy": 1,
        "store_facts": 14,
        "vendor_facts": 4,
        "wa_limits": 9,
        "weights_types": 49,
        "education": 5,
        "blogs": 3,
        "agent_prompts": 7,  # + the single-mode concierge (2026-10)
    }
    # Every seeded row was created except the four policy categories migration 0005 already made.
    assert seed.LAST_RUN["kept"] == len(seed.POLICY_CATEGORY_ROWS)
    created = _snapshot()
    assert seed.seed_all(refresh=True) == counts
    assert _snapshot() == created


@pytest.mark.django_db
def test_a_deleted_seed_row_comes_back_and_nothing_else_changes():
    """Create-only still heals a missing row (the reason the seed runs on every start)."""
    seed.seed_all()
    m.FAQEntry.objects.filter(key="payment").delete()
    m.FAQEntry.objects.filter(key="age-21").update(is_active=False)  # an owner switch-off
    seed.seed_all()
    assert m.FAQEntry.objects.filter(key="payment", is_active=True).exists()
    assert seed.LAST_RUN["created"] == 1
    assert m.FAQEntry.objects.get(key="age-21").is_active is False


def test_every_block_writes_through_the_one_helper():
    """No block may call the ORM write methods directly — only ``_seed`` decides create vs keep."""
    src = inspect.getsource(seed)
    helper = inspect.getsource(seed._seed)
    rest = src.replace(helper, "")
    assert not re.search(r"\.objects\.(update_or_create|get_or_create|create|bulk_create)\(", rest)
    assert not re.search(r"\.save\(|\.update\(", rest)
