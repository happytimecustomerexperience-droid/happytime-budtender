"""happytimeweed.com scraper for admin-controlled KB updates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from django.utils import timezone

from kb import semantic, vapi_files
from kb.models import BlogDoc, FAQEntry, PolicyDocument, SiteScrapeRun, StoreFact

BASE_URL = "https://happytimeweed.com"
TARGET_PATHS = ["/faq", "/specials", "/yakima", "/mount-vernon", "/pullman"]
_POISON_RE = re.compile(
    r"\b(ignore|disregard|override|reveal|print|show|leak)\b.{0,80}\b"
    r"(instruction|prompt|system|developer|secret|tool|policy|rule)s?\b",
    re.I | re.S,
)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_HOURS_RE = re.compile(r"Open Every(?:day| Day):?\s*([^|]+?)(?:Order Online|Visit Store|$)", re.I)


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str
    sections: list[tuple[str, str]]


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._heading = ""
        self._current = ""
        self._chunks: list[str] = []
        self.sections: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._flush_section()
            self._current = "heading"
        elif tag in {"p", "li"}:
            self._current = "body"

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "li"}:
            self._chunks.append("\n")
        elif tag in {"h1", "h2", "h3"}:
            self._current = ""

    def handle_data(self, data):
        if self.skip:
            return
        text = " ".join((data or "").split())
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        elif self._current == "heading":
            self._heading = f"{self._heading} {text}".strip()
            self._chunks.append(f"\n{text}\n")
        else:
            self._chunks.append(text + " ")

    def _flush_section(self):
        if not self._heading:
            return
        body = " ".join("".join(self._chunks).split())[-1800:]
        if body:
            self.sections.append((self._heading[:180], body))
        self._chunks = []

    def finish(self) -> Page:
        self._flush_section()
        text = " ".join("".join(self._chunks).split())
        return Page("", self.title, text, self.sections)


def _slug(value: str, *, limit: int = 80) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (out or "page")[:limit].strip("-")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse(url: str, html: str) -> Page:
    parser = _PageParser()
    parser.feed(html)
    page = parser.finish()
    text = " ".join((page.text or " ".join(body for _, body in page.sections)).split())
    return Page(url=url, title=page.title[:200], text=text[:6000], sections=page.sections)


def fetch_pages(paths: list[str] | None = None) -> list[Page]:
    pages = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for path in paths or TARGET_PATHS:
            url = urljoin(BASE_URL, path)
            resp = client.get(url, headers={"User-Agent": "HappyTimeVoiceBot/1.0"})
            resp.raise_for_status()
            pages.append(_parse(str(resp.url), resp.text))
    return pages


def _mark(obj, *, page: Page, status: str, error: str = ""):
    now = timezone.now()
    source_hash = _hash(obj.chunk_text())
    fields = ["source_url", "source_hash", "last_scraped_at", "scrape_status", "scrape_error"]
    obj.source_url = page.url
    obj.source_hash = source_hash
    obj.last_scraped_at = now
    obj.scrape_status = status
    obj.scrape_error = error[:300]
    obj.save(update_fields=[f for f in fields if hasattr(obj, f)] + ["updated_at"])


def _upsert_faq(page: Page, heading: str, body: str) -> tuple[str, object]:
    topic = "returns" if "return" in heading.lower() else "general"
    obj, created = FAQEntry.objects.update_or_create(
        key=f"site-{_slug(heading)}",
        defaults={
            "question": heading[:1000],
            "answer": body[:2000],
            "topic": topic,
            "weight": 105 if topic == "returns" else 80,
            "is_active": True,
        },
    )
    _mark(obj, page=page, status="applied")
    return ("created" if created else "updated", obj)


def _upsert_policy(page: Page, body: str) -> tuple[str, object]:
    obj, created = PolicyDocument.objects.update_or_create(
        kind="return_policy",
        defaults={
            "title": "Happy Time return policy",
            "body": body[:5000],
            "citation": "WAC 314-55-079" if "WAC 314-55-079" in body else "",
            "source_url": page.url,
            "weight": 120,
            "is_active": True,
        },
    )
    _mark(obj, page=page, status="applied")
    return ("created" if created else "updated", obj)


def _upsert_store_fact(page: Page, *, store: str, kind: str, label: str, value: str):
    obj, created = StoreFact.objects.update_or_create(
        store=store,
        kind=kind,
        label=label[:120],
        defaults={"value": value[:2000], "confirmed": True, "weight": 110, "is_active": True},
    )
    _mark(obj, page=page, status="applied")
    return ("created" if created else "updated", obj)


def _upsert_blog(page: Page):
    path = urlparse(page.url).path.strip("/")
    slug = _slug(path or page.title, limit=120)
    obj, created = BlogDoc.objects.update_or_create(
        slug=slug,
        defaults={
            "title": page.title or slug.replace("-", " ").title(),
            "body": page.text[:5000],
            "source_url": page.url,
            "provisional": False,
            "weight": 60,
            "is_active": True,
        },
    )
    _mark(obj, page=page, status="applied")
    return ("created" if created else "updated", obj)


def apply_pages(pages: list[Page]) -> tuple[dict, list[object]]:
    changes = {"created": 0, "updated": 0}
    touched = []
    for page in pages:
        path = urlparse(page.url).path
        lower = page.text.lower()
        if path.startswith("/faq"):
            for heading, body in page.sections:
                if "?" not in heading and not any(w in heading.lower() for w in ("return", "payment", "order")):
                    continue
                action, obj = _upsert_faq(page, heading, body)
                changes[action] += 1
                touched.append(obj)
                if "return" in heading.lower() or "return" in body.lower():
                    action, obj = _upsert_policy(page, body)
                    changes[action] += 1
                    touched.append(obj)
        elif path.startswith("/specials"):
            action, obj = _upsert_store_fact(
                page,
                store="",
                kind="special",
                label="Current website specials",
                value=page.text[:2000],
            )
            changes[action] += 1
            touched.append(obj)
        elif path in {"/yakima", "/mount-vernon", "/pullman"}:
            store = path.strip("/")
            hours = _HOURS_RE.search(page.text)
            if hours:
                action, obj = _upsert_store_fact(
                    page,
                    store=store,
                    kind="hours",
                    label=f"{store} hours",
                    value="Open Everyday: " + hours.group(1).strip()[:400],
                )
                changes[action] += 1
                touched.append(obj)
            phone = _PHONE_RE.search(page.text)
            if phone:
                action, obj = _upsert_store_fact(
                    page,
                    store=store,
                    kind="phone",
                    label=f"{store} phone",
                    value=phone.group(0),
                )
                changes[action] += 1
                touched.append(obj)
        elif "/blog/" in path and "cannabis" in lower:
            action, obj = _upsert_blog(page)
            changes[action] += 1
            touched.append(obj)
    return changes, touched


def validate_rows(rows: list[object]) -> list[str]:
    errors = []
    for row in rows:
        text = getattr(row, "answer", "") or getattr(row, "body", "") or getattr(row, "value", "")
        title = getattr(row, "key", "") or getattr(row, "label", "") or getattr(row, "title", "")
        if not str(text or "").strip():
            errors.append(f"{type(row).__name__} {row.pk} is empty")
        if _POISON_RE.search(str(text or "")):
            errors.append(f"{type(row).__name__} {row.pk} looks like prompt injection")
        critical = isinstance(row, PolicyDocument) or (
            isinstance(row, FAQEntry) and row.topic in {"returns", "limits", "age"}
        ) or (isinstance(row, StoreFact) and row.kind in {"limit", "age"})
        if critical and not getattr(row, "source_url", ""):
            errors.append(f"{type(row).__name__} {row.pk} {title!r} is critical but has no source")
    return errors


def run_scrape(*, publish: bool = True, paths: list[str] | None = None) -> SiteScrapeRun:
    run = SiteScrapeRun.objects.create(status="running")
    try:
        pages = fetch_pages(paths)
        changes, rows = apply_pages(pages)
        errors = validate_rows(rows)
        run.pages = [{"url": p.url, "title": p.title} for p in pages]
        run.changes = changes
        run.validation_errors = errors
        if errors:
            run.status = "blocked"
            run.summary = f"Blocked: {len(errors)} validation error(s)."
        else:
            chunks = semantic.reindex()
            mirror = vapi_files.mirror_all()
            publish_results = [{"object": "kb", "action": "reindexed", "chunks": chunks}]
            if mirror.get("skipped"):
                publish_results.append({"object": "vapi_files", "action": "skipped", "reason": mirror["skipped"]})
            else:
                publish_results.append({"object": "vapi_files", "action": "mirrored", "files": len(mirror.get("files", []))})
            if publish:
                from dashboard import publish as publish_mod

                publish_results.extend(r.to_dict() for r in publish_mod.publish_all())
            run.publish_results = publish_results
            run.status = "applied"
            run.summary = f"Applied {changes['created']} created, {changes['updated']} updated."
    except Exception as exc:  # noqa: BLE001 - keep admin audit instead of crashing silently.
        run.status = "failed"
        run.validation_errors = [str(exc)[:500]]
        run.summary = f"Failed: {type(exc).__name__}"
    run.finished_at = timezone.now()
    run.save()
    return run
