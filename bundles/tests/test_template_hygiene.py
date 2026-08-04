"""Template mistakes that reach the customer's screen.

These are cheap to make and expensive to miss: they don't raise, they don't fail a
view test that only checks status codes, and they look fine in a diff. They show up
as garbage on a live page — which is exactly how this file came to exist, after five
multi-line comment blocks shipped to production and rendered as body text above the
header on every storefront page.
"""
import pathlib
import re

from django.test import SimpleTestCase

TEMPLATES = sorted(
    (pathlib.Path(__file__).resolve().parent.parent / "templates" / "bundles").glob("*.html")
)

# {# ... #} spanning a newline. Django's comment tag is single-line ONLY; the
# closing #} on a later line is never matched, so the whole block renders verbatim.
MULTILINE_COMMENT = re.compile(r"\{#(?:(?!#\})[\s\S])*?\n(?:(?!#\})[\s\S])*?#\}")


class TemplateHygieneTests(SimpleTestCase):
    def test_templates_were_found(self):
        # Guard the guard: a bad glob would make every test below vacuously pass.
        self.assertGreaterEqual(len(TEMPLATES), 5, "no storefront templates found to check")

    def test_no_multiline_hash_comments(self):
        offenders = []
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            for m in MULTILINE_COMMENT.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
        self.assertEqual(
            offenders, [],
            "Multi-line {# #} renders as visible page text — Django's comment tag is "
            f"single-line only. Use {{% comment %}}…{{% endcomment %}}. Found at: {offenders}",
        )

    def test_the_detector_actually_detects(self):
        # A regex that matches nothing would make the test above pass forever.
        sample = "<p>ok</p>\n{# first line\n   second line #}\n<p>after</p>"
        self.assertTrue(MULTILINE_COMMENT.search(sample))
        self.assertFalse(MULTILINE_COMMENT.search("{# single line is fine #}"))

    def test_no_unclosed_block_tags_left_open(self):
        # A stray {% if %} without {% endif %} renders the rest of the page blank.
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            # Strip comment BODIES first. Prose inside a comment legitimately mentions
            # tag names (this file's own comments do), and counting those produced a
            # false "unbalanced" failure on a perfectly valid template.
            body = re.sub(r"\{%\s*comment\s*%\}[\s\S]*?\{%\s*endcomment\s*%\}", "", text)
            for tag in ("if", "for", "block", "with"):
                opens = len(re.findall(r"\{%\s*" + tag + r"[\s%]", body))
                closes = len(re.findall(r"\{%\s*end" + tag + r"\s*%\}", body))
                self.assertEqual(
                    opens, closes,
                    f"{path.name}: {opens} {{% {tag} %}} vs {closes} {{% end{tag} %}}",
                )
            # Comment tags themselves still have to balance, counted on the raw text
            # minus their own bodies — i.e. every opener consumed a closer above.
            self.assertEqual(
                len(re.findall(r"\{%\s*comment\s*%\}", body)), 0,
                f"{path.name}: unclosed {{% comment %}}",
            )
