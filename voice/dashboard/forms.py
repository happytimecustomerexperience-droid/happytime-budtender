"""Dashboard ModelForms (14-P4 §3.1, forms.py).

ModelForms for the editable KB rows (FAQ / policy / store-fact / education / blog / taxonomy), the
``RankingWeights`` tuner, and the ``VendorCallback`` status mutate. The form module shape ports from
swedish-bot/dashboard/forms.py; the fields are voice-domain. The weights form WARNS (never blocks)
when a weight set doesn't sum≈1.0 — owner override wins (global rules), but the tuner shows the
normalized preview budtender will apply.
"""

from __future__ import annotations

import json

from django import forms

from crm.models import VendorCallback
from kb.models import (
    BlogDoc,
    EducationDoc,
    FAQEntry,
    PolicyDocument,
    StoreFact,
    WeightTypeTaxonomy,
)

# slug (URL kind) → (model, label) for the KB source manager. The single registry the CRUD views
# and the KB-manager landing iterate, so a new KB kind is added in exactly one place.
KB_KINDS = {
    "faq": (FAQEntry, "FAQ"),
    "policy": (PolicyDocument, "Return / policy"),
    "store-fact": (StoreFact, "Store facts"),
    "education": (EducationDoc, "Education"),
    "blog": (BlogDoc, "Blogs"),
    "taxonomy": (WeightTypeTaxonomy, "Weights / types taxonomy"),
}


class FAQEntryForm(forms.ModelForm):
    class Meta:
        model = FAQEntry
        fields = ["key", "question", "answer", "store", "topic", "weight", "is_active"]


class PolicyForm(forms.ModelForm):
    class Meta:
        model = PolicyDocument
        fields = ["kind", "title", "body", "citation", "source_url", "weight", "is_active"]


class StoreFactForm(forms.ModelForm):
    class Meta:
        model = StoreFact
        fields = ["store", "kind", "label", "value", "confirmed", "weight", "is_active"]


class EducationDocForm(forms.ModelForm):
    class Meta:
        model = EducationDoc
        fields = [
            "slug",
            "title",
            "topic",
            "body",
            "source_url",
            "provisional",
            "weight",
            "is_active",
        ]


class BlogDocForm(forms.ModelForm):
    class Meta:
        model = BlogDoc
        fields = ["slug", "title", "body", "source_url", "provisional", "weight", "is_active"]


class TaxonomyForm(forms.ModelForm):
    class Meta:
        model = WeightTypeTaxonomy
        fields = ["axis", "term", "value", "notes", "weight", "is_active"]


# slug → ModelForm for the KB source manager.
KB_FORMS = {
    "faq": FAQEntryForm,
    "policy": PolicyForm,
    "store-fact": StoreFactForm,
    "education": EducationDocForm,
    "blog": BlogDocForm,
    "taxonomy": TaxonomyForm,
}


class RankingWeightsForm(forms.Form):
    """W_ANON/W_KNOWN as JSON text + the margin-emphasis knob. Validates JSON-object shape; WARNS
    (never blocks) on a non-1.0 sum — owner override wins."""

    w_anon = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}))
    w_known = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}))
    margin_emphasis = forms.FloatField(min_value=0.0, max_value=5.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warnings: list[str] = []

    def _clean_weights(self, field: str) -> dict:
        raw = (self.cleaned_data.get(field) or "").strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise forms.ValidationError(f"{field}: not valid JSON ({exc})") from exc
        if not isinstance(data, dict) or not all(
            isinstance(v, (int, float)) for v in data.values()
        ):
            raise forms.ValidationError(f"{field}: must be a JSON object of name → number")
        total = sum(float(v) for v in data.values())
        if abs(total - 1.0) > 0.01:  # WARN, do not block (owner override wins)
            self.warnings.append(f"{field} sums to {total:.2f}, not 1.0 — budtender will normalize")
        return {k: float(v) for k, v in data.items()}

    def clean_w_anon(self) -> dict:
        return self._clean_weights("w_anon")

    def clean_w_known(self) -> dict:
        return self._clean_weights("w_known")


class VendorCallbackForm(forms.ModelForm):
    class Meta:
        model = VendorCallback
        fields = ["status"]
