# Introduces PolicyCategory (owner-defined, no-code-change categories) and repoints
# PolicyDocument.kind (hardcoded, unique-per-kind) at a plain PolicyDocument.category FK.
#
# Data migration: production has live rows, including the real return policy — it must
# survive intact. We create one PolicyCategory per existing distinct `kind` value
# (using the same human labels the old POLICY_KINDS choices carried), repoint every
# existing PolicyDocument at its category, and give the return_policy category
# topic="return_policy" so today's topic-constrained retrieval behaviour is unchanged.
# The reverse migration recreates the `kind` values from `category.slug` so a rollback
# is non-destructive too.

import django.db.models.deletion
from django.db import migrations, models

_LABELS = {
    "return_policy": "Return policy",
    "privacy": "Privacy",
    "loyalty": "Loyalty terms",
    "other": "Other policy",
}


def _category_for(apps, slug):
    PolicyCategory = apps.get_model("kb", "PolicyCategory")
    label = _LABELS.get(slug, slug.replace("_", " ").title())
    topic = "return_policy" if slug == "return_policy" else ""
    category, _created = PolicyCategory.objects.get_or_create(
        slug=slug,
        defaults={"label": label, "topic": topic, "weight": 120, "is_active": True, "order": 0},
    )
    return category


def forwards(apps, schema_editor):
    PolicyDocument = apps.get_model("kb", "PolicyDocument")
    # Always create the four known categories up front (even with no rows yet), so a
    # fresh/empty DB still gets the historical vocabulary as a starting point for the owner.
    for slug in _LABELS:
        _category_for(apps, slug)
    for doc in PolicyDocument.objects.all():
        doc.category = _category_for(apps, doc.kind)
        doc.save(update_fields=["category"])


def backwards(apps, schema_editor):
    PolicyDocument = apps.get_model("kb", "PolicyDocument")
    for doc in PolicyDocument.objects.select_related("category").all():
        doc.kind = doc.category.slug if doc.category else "other"
        doc.save(update_fields=["kind"])


class Migration(migrations.Migration):

    dependencies = [
        ("kb", "0004_sitescraperun_blogdoc_last_scraped_at_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="PolicyCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=64, unique=True)),
                ("label", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("topic", models.CharField(blank=True, max_length=32)),
                ("weight", models.IntegerField(default=120)),
                ("is_active", models.BooleanField(default=True)),
                ("order", models.IntegerField(default=0)),
            ],
            options={
                "verbose_name_plural": "Policy categories",
                "ordering": ["order", "label"],
            },
        ),
        migrations.AddField(
            model_name="policydocument",
            name="category",
            field=models.ForeignKey(
                to="kb.policycategory",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                null=True,  # temporarily nullable so the data migration below can populate it
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="policydocument",
            name="category",
            field=models.ForeignKey(
                to="kb.policycategory",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
            ),
        ),
        migrations.RemoveField(
            model_name="policydocument",
            name="kind",
        ),
    ]
