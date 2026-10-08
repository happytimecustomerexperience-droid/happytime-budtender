"""Suggestion tracking (docs/contracts/suggestion-analytics-v1.md).

Every product we suggest (phone, website chat, questionnaire, find-similar, pairing) is stored as a
``SuggestedProduct`` with a frozen, customer-facing ``snapshot`` (never cost/margin) plus a
``SuggestionOutcome`` that says whether the customer bought it — or a SIBLING (same product line,
other strain/flavour and/or size) — within ``HHT_SUGGESTION_WINDOW_DAYS`` (default 10).

* ``record`` writes the rows for one request (two bulk INSERTs, no query per pick).
* ``attribute_lines`` is called from the transaction ingest (tasks._fold_history): event-driven,
  order-independent and idempotent (the earliest qualifying line wins; re-ingesting a line is a no-op).
* ``attach_sessions`` hands a session's anonymous suggestions to the customer it was linked to.
* ``close_expired`` (hourly) decides ``pending`` rows once the window + 1 day of sync lag has passed.
* ``match_history`` evaluates against ``purchase_history`` only where a stored timestamp proves a
  purchase inside the window (backfill / link / on-demand re-evaluation): it never guesses.

Request paths only read/write our own DB here; nothing calls Dutchie.

SIBLING KEY — ``sibling_key(name, brand, category, strain)``::

    brand_norm | category_family | product_line

* text is ASCII-folded, lowercased, "&"->"and", apostrophes dropped, "pre-roll"->"preroll",
  "cartridge(s)/carts"->"cart", "gummy"->"gummies"; punctuation other than ``.``/``:`` is a space.
* product_line = the name minus: the brand phrase, the product's OWN strain phrase, every size token
  (``10pk``, ``20 pack``, ``3.5g``, ``0.5 g``, ``1g``, ``100mg``, ``2x0.5g``, ``1/8 oz``, ``eighth``,
  ``single``...), strain-type words (indica/sativa/hybrid/dominant, ``(i)``/``(s)``/``(h)``) and
  compliance noise (``DOH``, ``approved``, ``compliant``). For edibles/beverages also flavour words
  (a fixed lexicon) and every word after the first form word (gummies, chocolate, bar, chews, mints,
  soda...) except ratios (``1:1``) and cannabinoid words (cbd/cbn/cbg/thcv), which name a formula.
* no brand -> ``""`` (never a sibling of anything).

Two products with the same non-empty key are siblings. The match kind compares
``size_signature`` (size tokens from the name, else unit_weight/potency_mg) and the strain/flavour
(``strain`` when both rows have one, else the name minus brand/size/noise):
size differs only -> ``sibling_size``; strain/flavour differs only -> ``sibling_strain``; both ->
``sibling_both``; neither (the same product under another SKU, e.g. another store) -> ``exact``.
An unknown size on either side is not a size difference.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SuggestedProduct, SuggestionOutcome

logger = logging.getLogger(__name__)

# ── channels ─────────────────────────────────────────────────────────────────
CHANNELS = ("phone", "chat", "questionnaire", "similar", "pairing", "menu", "unknown")
SOURCES = frozenset({"chat", "questionnaire", "similar", "pairing", "menu", "phone"})  # accepted from a client
# ChatSession.channel (chat|questionnaire|voice|web|menu) -> suggestion channel
_SESSION_CHANNEL = {"chat": "chat", "web": "chat", "questionnaire": "questionnaire", "menu": "menu",
                    "voice": "phone"}
_CALL_TOKEN = re.compile(r"vc-[A-Za-z0-9_-]{1,61}")

STATUSES = ("pending", "bought_exact", "bought_sibling", "not_bought", "unattributable")
BOUGHT = ("bought_exact", "bought_sibling")
MATCH_KINDS = ("exact", "sibling_size", "sibling_strain", "sibling_both")

SNAPSHOT_FIELDS = ("product_id", "name", "brand", "category", "subcategory", "strain", "strain_type",
                   "size_label", "unit_weight", "potency_mg", "price", "thc_percent", "slug", "rank",
                   "why", "kind", "source")
SNAPSHOT_FLAGS = ("snapshot_partial", "backfilled")
_STR_CAP = 255
_WHY_CAP = 300


def window() -> timedelta:
    days = getattr(settings, "HHT_SUGGESTION_WINDOW_DAYS", 10)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 10
    return timedelta(days=min(max(days, 1), 90))


GRACE = timedelta(days=1)  # transaction sync lag (the beat pulls every 6h)


def clean_source(value) -> str:
    v = str(value or "").strip().lower()[:16] if isinstance(value, str) else ""
    return v if v in SOURCES else ""


def phone_session(session) -> bool:
    if session is None:
        return False
    token = str(getattr(session, "session_token", "") or "")
    return getattr(session, "channel", "") == "voice" or bool(_CALL_TOKEN.fullmatch(token))


def resolve_channel(source, session, *, website: bool, caller_id: bool = False, default: str | None = None) -> str:
    """Which surface showed the suggestion. ``phone`` only ever from the backend token: a phone call's
    session (``channel="voice"`` / ``vc-<call id>`` token), the carrier caller-ID on the request, or an
    explicit ``source:"phone"``. Otherwise the allowlisted ``source`` the client sent, then the view's
    own default (similar/pairing), then the session's channel; ``unknown`` when nothing says."""
    src = clean_source(source)
    if not website and (phone_session(session) or caller_id or src == "phone"):
        return "phone"
    if src and src != "phone":
        return src
    if default:
        return default
    if session is not None:
        ch = _SESSION_CHANNEL.get(str(getattr(session, "channel", "") or ""), "unknown")
        return "unknown" if (website and ch == "phone") else ch
    return "unknown"


# ── text normalisation for the sibling key ───────────────────────────────────
_UNIT = {"mg": "mg", "g": "g", "gr": "g", "gm": "g", "gram": "g", "grams": "g", "oz": "oz", "ounce": "oz",
         "ounces": "oz", "ml": "ml", "pk": "pk", "pks": "pk", "pack": "pk", "packs": "pk", "ct": "pk",
         "count": "pk", "pc": "pk", "pcs": "pk", "piece": "pk", "pieces": "pk"}
_SIZE_RE = re.compile(
    r"(?<![a-z0-9.])(?:"
    r"(?P<mul>\d+)\s*x\s*(?P<mulv>\d+(?:\.\d+)?)\s*(?P<mulu>mg|g|gr|gm|grams?|ml)?"
    r"|(?P<num>\d+)\s*/\s*(?P<den>\d+)\s*(?:oz|ounces?)?"
    r"|(?P<v>\d+(?:\.\d+)?)\s*(?P<u>mg|gr|gm|grams?|g|ounces?|oz|ml|pks?|packs?|ct|count|pcs?|pieces?)"
    r")(?![a-z0-9])"
)
_SIZE_WORDS = {"eighth": "3.5g", "quarter": "7g", "half": "14g", "ounce": "28g", "zip": "28g"}
_SIZE_NOISE = {"single", "singles", "multipack", "each", "oz", "ounce"}
_STRAIN_TYPE = {"indica", "sativa", "hybrid", "ind", "sat", "hyb", "dominant", "dom"}
_NOISE = {"doh", "approved", "compliant", "dohc", "the", "by", "and", "with", "w", "new"}
_FLAVOURS = {
    "strawberry", "kiwi", "watermelon", "raspberry", "blueberry", "blackberry", "marionberry", "huckleberry",
    "boysenberry", "berry", "berries", "mango", "peach", "pear", "cherry", "grape", "apple", "orange",
    "tangerine", "lemon", "lime", "lemonade", "pineapple", "coconut", "banana", "passionfruit", "passion",
    "fruit", "fruity", "guava", "pomegranate", "cranberry", "plum", "apricot", "citrus", "tropical", "punch",
    "mixed", "assorted", "sour", "sweet", "cola", "root", "beer", "ginger", "peppermint", "spearmint",
    "vanilla", "caramel", "cinnamon", "honey", "cream", "original", "classic", "melon", "cucumber",
    "dragonfruit", "lychee", "yuzu", "grapefruit", "mint", "raz", "razz", "acai", "hibiscus",
}
_FORM_WORDS = {"gummies", "chocolate", "chocolates", "bar", "bars", "chews", "chew", "mints", "drops",
               "lozenges", "cookies", "cookie", "brownie", "brownies", "caramels", "taffy", "bites", "tablets",
               "tabs", "soda", "seltzer", "drink", "shot", "shots", "syrup", "lemonades", "tea", "coffee",
               "tonic", "elixir", "candy", "candies", "pastilles", "jellies", "jelly"}
_KEEP_AFTER_FORM = re.compile(r"^(?:\d+(?:\.\d+)?:\d+(?:\.\d+)?(?::\d+(?:\.\d+)?)?|cbd|cbn|cbg|thcv|cbc)$")
_FAMILY = {"flower": "flower", "pre-rolls": "preroll", "blunt": "preroll", "infused-blunt": "preroll",
           "concentrates": "concentrate", "vape-cartridges": "vape", "edibles": "edible", "mints": "edible",
           "beverages": "beverage", "tinctures": "tincture", "topicals": "topical", "capsules": "capsule"}
_EDIBLE_FAMILIES = {"edible", "beverage"}


def _fold(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"['`’]", "", s)
    s = re.sub(r"\bpre[\s-]*rolls?\b", "preroll", s)
    s = re.sub(r"\b(?:cartridges?|carts?)\b", "cart", s)
    s = re.sub(r"\bgummy\b", "gummies", s)
    s = re.sub(r"\((?:i|s|h|ind|sat|hyb|indica|sativa|hybrid)\)", " ", s)
    s = re.sub(r"[^a-z0-9.:/]+", " ", s)
    # a dot or colon that is not inside a number is punctuation
    s = re.sub(r"(?<!\d)[.:]|[.:](?!\d)", " ", s)
    return " ".join(s.split())


def _phrase_out(text: str, phrase: str) -> str:
    phrase = _fold(phrase)
    if not phrase:
        return text
    return " ".join(re.sub(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", " ", text).split())


def family(category) -> str:
    c = str(category or "").strip().lower()
    return _FAMILY.get(c, _fold(c).replace(" ", "-"))


def _sizes_out(text: str) -> str:
    text = _SIZE_RE.sub(" ", text)
    return " ".join(t for t in text.split() if t not in _SIZE_WORDS and t not in _SIZE_NOISE)


def _num(v) -> str:
    return f"{float(v):g}"


def size_signature(name, unit_weight=None, potency_mg=None, category=None) -> frozenset:
    """The size tokens of a product, normalised ("10pk", "3.5g", "100mg"): from the name, else from the
    stored unit weight / potency. Empty = unknown."""
    text = _fold(name)
    out: set[str] = set()
    for m in _SIZE_RE.finditer(text):
        if m.group("mul"):
            unit = _UNIT.get(m.group("mulu") or "g", "g")
            out.add(f"{m.group('mul')}x{_num(m.group('mulv'))}{unit}")
        elif m.group("num"):
            num, den = int(m.group("num")), int(m.group("den"))
            if den:
                out.add(f"{_num(round(28 * num / den, 1))}g")
        else:
            unit = _UNIT.get(m.group("u"), m.group("u"))
            v = float(m.group("v"))
            out.add(f"{_num(v * 28)}g" if unit == "oz" else f"{_num(v)}{unit}")
    for t in text.split():
        if t in _SIZE_WORDS:
            out.add(_SIZE_WORDS[t])
    if not out:
        fam = family(category)
        if unit_weight and fam not in _EDIBLE_FAMILIES:
            out.add(f"{_num(unit_weight)}g")
        if potency_mg and fam in _EDIBLE_FAMILIES | {"tincture", "capsule"}:
            out.add(f"{_num(potency_mg)}mg")
    return frozenset(out)


def _descriptor(name, brand) -> str:
    """The name minus brand, sizes, strain-type and compliance noise: what remains names the strain or
    flavour (used only to tell siblings apart, never to decide one)."""
    text = _sizes_out(_phrase_out(_fold(name), brand))
    return " ".join(t for t in text.split() if t not in _NOISE and t not in _STRAIN_TYPE)


def product_line(name, brand, category, strain="") -> str:
    fam = family(category)
    text = _phrase_out(_fold(name), brand)
    if strain:
        text = _phrase_out(text, strain)
    text = _sizes_out(text)
    words = [t for t in text.split() if t not in _NOISE and t not in _STRAIN_TYPE]
    if fam in _EDIBLE_FAMILIES:
        words = [t for t in words if t not in _FLAVOURS or t in _FORM_WORDS]
        for i, t in enumerate(words):
            if t in _FORM_WORDS:
                words = words[:i + 1] + [w for w in words[i + 1:] if _KEEP_AFTER_FORM.match(w)]
                break
    return " ".join(words)


def sibling_key(name, brand, category, strain="") -> str:
    b = _fold(brand)
    if not b or not str(name or "").strip():
        return ""
    return f"{b}|{family(category)}|{product_line(name, brand, category, strain)}"[:255]


def _same_strain(a: dict, b: dict) -> bool:
    sa, sb = _fold(a.get("strain")), _fold(b.get("strain"))
    if sa and sb:
        return sa == sb
    return _descriptor(a.get("name"), a.get("brand")) == _descriptor(b.get("name"), b.get("brand"))


def match_kind(suggested: dict, bought: dict, suggested_key: str | None = None) -> str:
    """``exact`` | ``sibling_size`` | ``sibling_strain`` | ``sibling_both`` | "" for one suggestion
    (snapshot dict + sku) against one purchased product (same keys). See the module doc."""
    for f in ("product_id", "sku"):
        x, y = str(suggested.get(f) or ""), str(bought.get(f) or "")
        if x and y and x == y:
            return "exact"
    key = suggested_key if suggested_key is not None else _key_of(suggested)
    if not key or key != _key_of(bought):
        return ""
    sa = size_signature(suggested.get("name"), suggested.get("unit_weight"), suggested.get("potency_mg"),
                        suggested.get("category"))
    sb = size_signature(bought.get("name"), bought.get("unit_weight"), bought.get("potency_mg"),
                        bought.get("category"))
    size_differs = bool(sa and sb and sa != sb)
    strain_differs = not _same_strain(suggested, bought)
    if size_differs and strain_differs:
        return "sibling_both"
    if size_differs:
        return "sibling_size"
    if strain_differs:
        return "sibling_strain"
    return "exact"


def _key_of(d: dict) -> str:
    if "_key" in d:
        return d["_key"]
    return sibling_key(d.get("name"), d.get("brand"), d.get("category"), d.get("strain"))


# ── snapshot + recording ─────────────────────────────────────────────────────
def _scalar(v, cap: int = _STR_CAP):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v).replace("\x00", "")[:cap]


def public_snapshot(snap) -> dict:
    """A stored snapshot as a response may carry it: the allowlisted keys only."""
    snap = snap if isinstance(snap, dict) else {}
    return {k: snap[k] for k in (*SNAPSHOT_FIELDS, "sku", *SNAPSHOT_FLAGS) if k in snap}


def snapshot(p, pub: dict | None, *, rank, why, kind: str, channel: str) -> dict:
    """What the customer was shown, frozen: the public card (price/THC as served) + the Product row's
    customer-facing attributes. Built from objects already in memory — no query. Never cost/margin."""
    from .ranking import size_label

    pub = pub if isinstance(pub, dict) else {}
    g = lambda f, d="": getattr(p, f, d)  # noqa: E731
    size = (pub.get("size") or g("subcategory")
            or size_label(g("unit_weight", None), g("potency_mg", None), g("category"))
            or " ".join(sorted(size_signature(pub.get("name") or g("name")))))  # "10pk", "100mg 10pk"
    snap = {
        "product_id": g("product_id"), "name": pub.get("name") or g("name"), "brand": pub.get("brand") or g("brand"),
        "category": g("category"), "subcategory": g("subcategory"), "strain": pub.get("strain") or g("strain"),
        "strain_type": g("strain_type"), "size_label": size or "", "unit_weight": g("unit_weight", None),
        "potency_mg": g("potency_mg", None), "price": pub.get("price", g("price", None)),
        "thc_percent": pub.get("thc_percent", g("thc_percent", None)), "slug": g("slug"),
        "rank": rank, "why": _scalar(why or "", _WHY_CAP), "kind": kind, "source": channel,
    }
    return {k: _scalar(v) for k, v in snap.items()}


def _key_for(snap: dict) -> str:
    return sibling_key(snap.get("name"), snap.get("brand"), snap.get("category"), snap.get("strain"))


def record(*, session, customer, location: str, picks: list, kind: str, channel: str,
           identity_via: str = "", legacy_source: str = "chat", paired_with_sku: str = "",
           reason_code: str = "") -> list:
    """One request's suggestions: ``picks`` is ``[(Product, public dict), ...]`` in display order (the
    public dict's ``rank``/``why_this`` are used). Two bulk INSERTs (rows, then their outcomes)."""
    if not picks:
        return []
    channel = channel if channel in CHANNELS else "unknown"
    via = str(identity_via or "")[:16] if customer is not None else ""
    rows = []
    for i, (p, pub) in enumerate(picks):
        pub = pub if isinstance(pub, dict) else {}
        snap = snapshot(p, pub, rank=pub.get("rank", i + 1), why=pub.get("why_this"), kind=kind, channel=channel)
        rows.append(SuggestedProduct(
            session=session, customer=customer, location_slug=str(location or "")[:32],
            sku=str(pub.get("sku") or getattr(p, "sku", "") or "")[:64], kind=kind,
            source=str(legacy_source or "chat")[:16], paired_with_sku=str(paired_with_sku or "")[:64],
            reason_code=str(reason_code or "")[:32], snapshot=snap, sibling_key=_key_for(snap),
            channel=channel, identity_via=via,
        ))
    win = window()
    with transaction.atomic():
        created = SuggestedProduct.objects.bulk_create(rows)
        SuggestionOutcome.objects.bulk_create(
            [SuggestionOutcome(suggestion=r, window_ends_at=(r.shown_at or timezone.now()) + win) for r in created])
    return created


def record_safely(**kw) -> list:
    """``record`` for a request path: a failure to log a suggestion never costs the customer the answer."""
    try:
        return record(**kw)
    except Exception:  # noqa: BLE001
        logger.warning("suggestion recording failed", exc_info=True)
        return []


# ── attribution ──────────────────────────────────────────────────────────────
def _aware(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt_timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _money(v):
    try:
        return Decimal(str(round(float(v), 2))) if v not in (None, "") else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def _suggested_dict(sp) -> dict:
    snap = sp.snapshot if isinstance(sp.snapshot, dict) else {}
    return {**snap, "sku": sp.sku, "_key": sp.sibling_key}


def _rank_key(at, kind: str, line: str) -> tuple:
    return (at, 0 if kind == "exact" else 1, line)


def _apply(outcome, *, kind: str, at, line: str, sku: str, product_id: str, name: str, amount, now) -> bool:
    """Set the match if it beats the stored one (earlier wins; same instant: exact over sibling, then the
    line id). Returns True when something changed. Re-applying the same line changes nothing."""
    if outcome.matched_line == line and outcome.status in BOUGHT:
        return False
    if outcome.status in BOUGHT and outcome.matched_at is not None:
        if _rank_key(outcome.matched_at, outcome.match_kind, outcome.matched_line) <= _rank_key(at, kind, line):
            return False
    outcome.status = "bought_exact" if kind == "exact" else "bought_sibling"
    outcome.match_kind = kind
    outcome.matched_at = at
    outcome.matched_line = line[:160]
    outcome.matched_sku = str(sku or "")[:64]
    outcome.matched_product_id = str(product_id or "")[:64]
    outcome.matched_name = str(name or "")[:255]
    outcome.matched_amount = amount
    outcome.evaluated_at = now
    return True


_OUTCOME_FIELDS = ["status", "match_kind", "matched_at", "matched_line", "matched_sku", "matched_product_id",
                   "matched_name", "matched_amount", "evaluated_at"]


def _line_product(ln: dict) -> dict:
    return {"product_id": ln.get("product_id") or "", "sku": ln.get("sku") or "",
            "name": ln.get("product_name") or ln.get("_line_name") or "",
            "brand": ln.get("brand") or ln.get("_line_brand") or "", "category": ln.get("category") or "",
            "strain": ln.get("strain") or "", "strain_type": ln.get("strain_type") or "",
            "unit_weight": ln.get("unit_weight"), "potency_mg": ln.get("potency_mg")}


def attribute_lines(profile, lines: list[dict], *, now=None) -> int:
    """Match ingested transaction lines (tasks.sync_transactions -> _fold_history) against this
    customer's suggestions: every suggestion with ``shown_at <= line time <= window_ends_at`` that the
    line is the product (``bought_exact``) or a sibling of (``bought_sibling``). Order-independent and
    idempotent. Returns the number of outcomes changed."""
    if profile is None or not lines:
        return 0
    now = now or timezone.now()
    parsed = []
    for i, ln in enumerate(lines):
        at = _aware(ln.get("bought_at"))
        if at is None:
            continue
        prod = _line_product(ln)
        prod["_key"] = _key_of(prod)
        line_id = str(ln.get("tx_line") or f"{ln.get('bought_at')}:{prod['product_id'] or prod['sku']}:{i}")
        amount = ln.get("line_total")
        if amount is None:
            amount = float(ln.get("last_price") or 0) * float(ln.get("qty") or 1)
        parsed.append((at, line_id, prod, _money(amount)))
    if not parsed:
        return 0
    lo, hi = min(p[0] for p in parsed), max(p[0] for p in parsed)
    outcomes = list(SuggestionOutcome.objects.select_related("suggestion")
                    .filter(suggestion__customer=profile, suggestion__shown_at__lte=hi, window_ends_at__gte=lo))
    changed = []
    for o in outcomes:
        sp = o.suggestion
        sug = _suggested_dict(sp)
        best = None
        for at, line_id, prod, amount in parsed:
            if not (sp.shown_at <= at <= o.window_ends_at):
                continue
            kind = match_kind(sug, prod, sp.sibling_key)
            if not kind:
                continue
            cand = (_rank_key(at, kind, line_id), kind, at, line_id, prod, amount)
            if best is None or cand[0] < best[0]:
                best = cand
        if best is not None:
            _, kind, at, line_id, prod, amount = best
            if _apply(o, kind=kind, at=at, line=line_id, sku=prod["sku"], product_id=prod["product_id"],
                      name=prod["name"], amount=amount, now=now):
                changed.append(o)
    if changed:
        SuggestionOutcome.objects.bulk_update(changed, _OUTCOME_FIELDS, batch_size=500)
    return len(changed)


def match_history(profile, pairs: list, *, now=None) -> list:
    """Evaluate (suggestion, outcome) pairs against ``profile.purchase_history`` where it gives
    CERTAINTY: an entry's ``first_bought_at`` or ``last_bought_at`` lies in [shown_at, window_ends_at].
    Mutates the outcomes in memory and returns the ones that changed (the caller saves them)."""
    now = now or timezone.now()
    hist = []
    for h in (getattr(profile, "purchase_history", None) or []):
        if not isinstance(h, dict):
            continue
        prod = _line_product(h)
        prod["_key"] = _key_of(prod)
        stamps = []
        for f in ("first_bought_at", "last_bought_at"):
            at = _aware(h.get(f)) if h.get(f) else None
            if at is not None:
                stamps.append((f, at))
        if stamps:
            hist.append((prod, stamps, h))
    changed = []
    for sp, o in pairs:
        if o.status in BOUGHT:
            continue
        sug = _suggested_dict(sp)
        best = None
        for prod, stamps, h in hist:
            kind = None
            for f, at in stamps:
                if not (sp.shown_at <= at <= o.window_ends_at):
                    continue
                kind = kind if kind is not None else match_kind(sug, prod, sp.sibling_key)
                if not kind:
                    break
                line = f"history:{prod['product_id'] or prod['sku']}:{at.isoformat()}"
                amount = _money(h.get("last_price")) if f == "last_bought_at" else None
                cand = (_rank_key(at, kind, line), kind, at, line, prod, amount)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is not None:
            _, kind, at, line, prod, amount = best
            if _apply(o, kind=kind, at=at, line=line, sku=prod["sku"], product_id=prod["product_id"],
                      name=prod["name"], amount=amount, now=now):
                changed.append(o)
    return changed


def close_expired(*, now=None, ids=None) -> dict:
    """``pending`` rows whose window + 1 day grace has passed: ``not_bought`` when the customer is
    known, else ``unattributable`` (never ``not_bought`` for someone we could not see buy)."""
    now = now or timezone.now()
    qs = SuggestionOutcome.objects.filter(status="pending", window_ends_at__lt=now - GRACE)
    if ids is not None:
        qs = qs.filter(suggestion_id__in=list(ids))
    nb = qs.filter(suggestion__customer__isnull=False).update(status="not_bought", evaluated_at=now)
    ua = qs.filter(suggestion__customer__isnull=True).update(status="unattributable", evaluated_at=now)
    return {"not_bought": nb, "unattributable": ua}


def attach_sessions(sessions, profile, via: str, *, now=None) -> int:
    """A session (queryset or ids) was tied to ``profile``: its anonymous suggestions become that
    customer's (and every one of its rows for that customer gets ``identity_via``). Rows already
    attributed to SOMEONE ELSE are never moved (a session that changed hands).

    Purchases made before the link were ingested while the row had no customer, so the attached rows
    are checked against purchase history (certainty only). A row whose window is still open stays
    ``pending`` (every purchase since shown_at is visible as a last_bought_at <= now, and later ones
    arrive through the ingest); a row already closed as ``unattributable`` changes only on proof."""
    if profile is None:
        return 0
    now = now or timezone.now()
    ids = list(SuggestedProduct.objects.filter(session__in=sessions)
               .filter(customer__isnull=True).values_list("id", flat=True))
    via = str(via or "")[:16]
    with transaction.atomic():
        if ids:
            SuggestedProduct.objects.filter(id__in=ids).update(customer=profile, identity_via=via)
        SuggestedProduct.objects.filter(session__in=sessions, customer=profile).exclude(
            identity_via=via).update(identity_via=via)
        if not ids:
            return 0
        pairs = [(o.suggestion, o) for o in SuggestionOutcome.objects.select_related("suggestion")
                 .filter(suggestion_id__in=ids)]
        # The caller's profile object may predate the latest ingest: read the history as stored.
        from .models import CustomerProfile

        stored = CustomerProfile.objects.filter(pk=profile.pk).only("id", "purchase_history").first()
        changed = match_history(stored, pairs, now=now) if stored is not None else []
        if changed:
            SuggestionOutcome.objects.bulk_update(changed, _OUTCOME_FIELDS)
    return len(ids)


def attach_sessions_safely(sessions, profile, via: str) -> int:
    try:
        return attach_sessions(sessions, profile, via)
    except Exception:  # noqa: BLE001 - linking a session must never fail on the analytics side
        logger.warning("attaching suggestions to a linked session failed", exc_info=True)
        return 0


# ── backfill ─────────────────────────────────────────────────────────────────
_LEGACY_SOURCE = {"chat": "chat", "web": "chat", "questionnaire": "questionnaire", "menu": "menu",
                  "catalog": "menu", "voice": "phone"}


def legacy_channel(sp) -> str:
    """Best channel for a row written before ``channel`` existed: a phone session wins, then the kind
    (pairing), then the old ``source`` (which held the session's channel)."""
    if phone_session(sp.session) or sp.source == "voice":
        return "phone"
    if sp.kind == "pairing":
        return "pairing"
    return _LEGACY_SOURCE.get(sp.source or "", "unknown")


def backfill(*, apply: bool, now=None, batch: int = 500) -> dict:
    """Give rows written before v1 a snapshot (from the Product row when the SKU still exists, else
    ``snapshot_partial``), a channel, identity_via and an outcome. Outcomes are decided ONLY with
    certainty from purchase_history; an expired window without that proof is ``unattributable``."""
    from .models import CustomerProfile, Product

    now = now or timezone.now()
    win = window()
    stats = {"rows": 0, "snapshots": 0, "partial": 0, "outcomes_created": 0, "bought_exact": 0,
             "bought_sibling": 0, "pending": 0, "unattributable": 0}
    todo = (SuggestedProduct.objects.filter(outcome__isnull=True)
            | SuggestedProduct.objects.filter(snapshot={})).distinct()
    ids = list(todo.order_by("id").values_list("id", flat=True))
    for start in range(0, len(ids), batch):
        chunk = list(SuggestedProduct.objects.select_related("session").filter(id__in=ids[start:start + batch]))
        skus = {sp.sku for sp in chunk}
        by_loc, by_sku = {}, {}
        for p in Product.objects.filter(sku__in=skus).order_by("-availability", "id"):
            by_loc.setdefault((p.location_slug, p.sku), p)
            by_sku.setdefault(p.sku, p)
        existing = {o.suggestion_id: o for o in SuggestionOutcome.objects.filter(suggestion_id__in=[s.id for s in chunk])}
        profiles = {p.pk: p for p in CustomerProfile.objects.filter(
            pk__in={s.customer_id for s in chunk if s.customer_id})}
        rows_changed, new_outcomes, pairs_by_cust = [], [], {}
        for sp in chunk:
            stats["rows"] += 1
            if not sp.snapshot:
                p = by_loc.get((sp.location_slug, sp.sku)) or by_sku.get(sp.sku)
                if p is not None:
                    sp.snapshot = {**snapshot(p, None, rank=None, why="", kind=sp.kind, channel=legacy_channel(sp)),
                                   "backfilled": True}
                    sp.sibling_key = _key_for(sp.snapshot)
                else:
                    sp.snapshot = {"sku": sp.sku, "kind": sp.kind, "source": legacy_channel(sp),
                                   "snapshot_partial": True, "backfilled": True}
                    sp.sibling_key = ""
                    stats["partial"] += 1
                stats["snapshots"] += 1
            if sp.channel == "unknown":
                sp.channel = legacy_channel(sp)
            if sp.customer_id and not sp.identity_via and sp.session is not None:
                sp.identity_via = (sp.session.identity_via or "")[:16]
            rows_changed.append(sp)
            o = existing.get(sp.id)
            if o is None:
                end = sp.shown_at + win
                o = SuggestionOutcome(suggestion=sp, window_ends_at=end, evaluated_at=now,
                                      status="pending" if now <= end + GRACE else "unattributable")
                new_outcomes.append(o)
                if sp.customer_id in profiles:
                    pairs_by_cust.setdefault(sp.customer_id, []).append((sp, o))
        for cid, pairs in pairs_by_cust.items():
            match_history(profiles[cid], pairs, now=now)
        for o in new_outcomes:
            stats[o.status] = stats.get(o.status, 0) + 1
        stats["outcomes_created"] += len(new_outcomes)
        if apply:
            with transaction.atomic():
                SuggestedProduct.objects.bulk_update(rows_changed, ["snapshot", "sibling_key", "channel",
                                                                    "identity_via"], batch_size=500)
                SuggestionOutcome.objects.bulk_create(new_outcomes, batch_size=500)
    return stats


def evaluate(*, apply: bool, days: int = 30, now=None) -> dict:
    """Re-evaluate undecided (pending / not_bought / unattributable) outcomes of known customers shown in
    the last ``days`` against purchase history (certainty only), then close expired windows."""
    from .models import CustomerProfile

    now = now or timezone.now()
    qs = (SuggestionOutcome.objects.select_related("suggestion")
          .filter(suggestion__shown_at__gte=now - timedelta(days=days), suggestion__customer__isnull=False)
          .exclude(status__in=BOUGHT))
    pairs_by_cust: dict[int, list] = {}
    for o in qs.iterator(chunk_size=500):
        pairs_by_cust.setdefault(o.suggestion.customer_id, []).append((o.suggestion, o))
    changed = []
    profiles = CustomerProfile.objects.in_bulk(list(pairs_by_cust))
    for cid, pairs in pairs_by_cust.items():
        if cid in profiles:
            changed += match_history(profiles[cid], pairs, now=now)
    out = {"examined": sum(len(p) for p in pairs_by_cust.values()), "newly_bought": len(changed),
           "bought_exact": sum(o.status == "bought_exact" for o in changed),
           "bought_sibling": sum(o.status == "bought_sibling" for o in changed)}
    expired = SuggestionOutcome.objects.filter(status="pending", window_ends_at__lt=now - GRACE)
    out["would_close"] = expired.count()
    if apply:
        if changed:
            SuggestionOutcome.objects.bulk_update(changed, _OUTCOME_FIELDS, batch_size=500)
        out["closed"] = close_expired(now=now)
    return out
