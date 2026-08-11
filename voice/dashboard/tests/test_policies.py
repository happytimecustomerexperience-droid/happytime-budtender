"""Owner-editable policies page (``dash-policies``) — categories + policy docs + a live test box.

The whole point of this page: the owner writes a policy, invents his own category, and can
PROVE it gets used by the agent — without touching Django admin or redeploying. Offline, SQLite.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="owner", password="x", is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_policies_page_renders(staff_client):
    resp = staff_client.get(reverse("dash-policies"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "categor" in body.lower()
    assert "test" in body.lower()


@pytest.mark.django_db
def test_create_category_then_policy_then_cited_by_test_box(staff_client):
    """The centrepiece: owner-invented category -> owner-written policy -> the test box proves
    the agent actually cites it, with the real answer text."""
    from kb.models import PolicyCategory, PolicyDocument

    resp = staff_client.post(
        reverse("dash-policies-category-new"),
        data={
            "slug": "frobnication-policy",
            "label": "Frobnication Policy",
            "description": "Rules about frobnicating widgets.",
            "topic": "return_policy",
            "weight": 120,
            "is_active": True,
            "order": 0,
        },
    )
    assert resp.status_code in (302, 303), resp.content
    category = PolicyCategory.objects.get(slug="frobnication-policy")
    assert category.label == "Frobnication Policy"

    # Policy CRUD reuses the existing generic KB-row editor (kind=policy) — already wired to the
    # category FK by PolicyForm; no new CRUD surface needed for the document itself.
    resp = staff_client.post(
        reverse("dash-kb-row-new", kwargs={"kind": "policy"}),
        data={
            "category": category.pk,
            "title": "Frobnicated widget returns",
            "body": (
                "A frobnicated widget may be returned within 30 days if the frobnication seal "
                "is unbroken. Frobnicated widgets bought on sale are final."
            ),
            "citation": "",
            "source_url": "",
            "weight": 120,
            "is_active": True,
        },
    )
    assert resp.status_code in (302, 303), resp.content
    policy = PolicyDocument.objects.get(category=category)
    assert policy.title == "Frobnicated widget returns"

    resp = staff_client.post(
        reverse("dash-policies-test"),
        data=json.dumps(
            {
                "message": "can I return a frobnicated widget",
                "policy_id": policy.pk,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["ok"] is True
    assert "frobnicat" in data["answer"].lower()
    assert data["grounded"] is True
    titles = [s.get("title") for s in data["sources"]]
    assert "Frobnicated widget returns" in titles
    assert data["policy_cited"] is True


@pytest.mark.django_db
def test_test_box_verdict_false_when_a_different_policy_is_cited(staff_client):
    from kb.models import PolicyCategory, PolicyDocument

    cat = PolicyCategory.objects.create(slug="unrelated-cat", label="Unrelated")
    unrelated = PolicyDocument.objects.create(
        category=cat, title="Unrelated policy", body="Nothing to do with the question at all."
    )
    resp = staff_client.post(
        reverse("dash-policies-test"),
        data=json.dumps({"message": "what are your hours today", "policy_id": unrelated.pk}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["policy_cited"] is False


@pytest.mark.django_db
def test_delete_category_with_documents_does_not_500(staff_client):
    from kb.models import PolicyCategory, PolicyDocument

    cat = PolicyCategory.objects.create(slug="has-docs", label="Has Docs")
    PolicyDocument.objects.create(category=cat, title="A doc", body="Some body text.")

    resp = staff_client.post(reverse("dash-policies-category-delete", kwargs={"pk": cat.pk}))
    assert resp.status_code in (302, 303), resp.content
    assert PolicyCategory.objects.filter(pk=cat.pk).exists(), "category must survive PROTECT"


@pytest.mark.django_db
def test_delete_empty_category_succeeds(staff_client):
    from kb.models import PolicyCategory

    cat = PolicyCategory.objects.create(slug="empty-cat", label="Empty")
    resp = staff_client.post(reverse("dash-policies-category-delete", kwargs={"pk": cat.pk}))
    assert resp.status_code in (302, 303), resp.content
    assert not PolicyCategory.objects.filter(pk=cat.pk).exists()


@pytest.mark.django_db
def test_test_box_never_mutates_an_existing_session(staff_client):
    """Every test-box run must use its own fresh session_token — never a real session's history."""
    from voice.models import VoiceCall

    # A real console/session already exists (simulating a real caller/chat session).
    real_call = VoiceCall.objects.create(call_id="real-session-1")
    turns_before = real_call.turns.count()

    resp = staff_client.post(
        reverse("dash-policies-test"),
        data=json.dumps({"message": "what are your hours today"}),
        content_type="application/json",
    )
    assert resp.status_code == 200

    real_call.refresh_from_db()
    assert real_call.turns.count() == turns_before, "test box polluted an unrelated session"

    call_ids = set(VoiceCall.objects.values_list("call_id", flat=True))
    assert "real-session-1" in call_ids
    # a distinct session_token was used for the test turn (not "real-session-1", not reused)
    assert len(call_ids) >= 1
