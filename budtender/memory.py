"""Customer memory v1 (docs/contracts/customer-memory-v1.md): the ONE place memory is validated,
merged and turned into the short ``brief`` both bots read (website chat and the Vapi phone agent).

Memory is PERSONAL data. What goes in passes an allowlist (closed keys, enum styles, short strings
that clear the quarantine rules below) and a 4 KB cap; what comes out (``brief``) is plain text of at
most 600 characters, and the ``public`` variant for an UNVERIFIED reader (a typed website phone) carries
only purchase-backed taste and style: never notes, stated likes/dislikes, context or recent topics,
and conversation summaries only when the owner sets HHT_MEMORY_WEB_SUMMARIES. Summaries
(``summaries``/``summary``) are written by budtender.memory_summary; ``echoes`` guards reply paths so
the bot never reads memory back to the customer.

Who may read/write which tier is decided by ``identity.tier(session)``; this module never looks at a
request. Pure apart from ``set_derived`` (one locked UPDATE).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)

VERSION = 1
MAX_BYTES = 4096          # hard cap of the serialized memory (contract)
BRIEF_MAX = 600           # brief text cap (contract)

TRUSTED, UNVERIFIED, ANONYMOUS = "trusted", "unverified", "anonymous"
TIERS = (TRUSTED, UNVERIFIED, ANONYMOUS)

STYLE_ENUMS = {
    "length": ("short", "medium", "long"),
    "tone": ("casual", "neutral", "formal"),
    "pace": ("quick", "browse"),
}
STYLE_BOOLS = ("emoji", "wants_explanations")

# list key -> (max items, max chars per item). Newest last; trimming keeps the newest.
LISTS = {"likes": (8, 40), "dislikes": (8, 40), "context": (4, 60), "last_topics": (4, 40)}
NOTES_MAX, NOTE_CHARS = 8, 120
SOURCES = ("voice", "chat")
# Conversation summaries (customer-memory-v1 "Summaries"): ``summaries`` = short AI notes, one per
# conversation, newest last; ``summary`` = the ONE consolidated string they are folded into.
SUMMARY_ENTRY_CHARS, SUMMARY_CHARS = 240, 500
SUMMARIES_MAX = 20        # hard ceiling; consolidation (HHT_MEMORY_CONSOLIDATE_AT, default 10) keeps it lower
# Session-only bookkeeping (ChatSession.learned): which turns were already learned (idempotency);
# ``sdigest``/``skey`` = which turns were summarised and which profile entry that summary is.
SESSION_META = ("digest", "ldigest", "upto", "sdigest", "skey")

# ── quarantine: a string that may be stored ───────────────────────────────────
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")
_PII = (
    re.compile(r"\d[\d\s().\-]{5,}\d"),                       # phone / ID / card / SSN-like digit runs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                     # SSN
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),          # dates (DOB)
    re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}\.\w+"),           # email
    re.compile(r"\b\d{1,5}\s+(?:[NSEW]{1,2}\s+)?(?:\w+\s+){1,3}(?:st|street|ave|avenue|rd|road|blvd|dr|drive|"
               r"ln|lane|ct|court|way|pl|place|hwy|highway)\b", re.I),   # street address
    re.compile(r"\[redacted\]|\bredacted\b", re.I),
    re.compile(r"\b(ssn|social security|date of birth|dob|birthday|born on|driver'?s? licen[cs]e|passport|"
               r"id number|credit card|debit card|card number|cvv|routing number|account number|bank|venmo|"
               r"paypal|zelle|address|apartment|apt)\b", re.I),
)
# Anything shaped like an instruction to the bot, markup, links or secrets.
_INJECTION = re.compile(
    r"\b(ignore|disregard|override|bypass|jailbreak|pretend|roleplay|role-play|act as|you are now|"
    r"from now on|system|developer|admin\w*|root|prompt\w*|instruction\w*|polic(y|ies)|rules?|"
    r"password\w*|passcode|credential\w*|api[\s_-]?keys?|tokens?|secrets?|remember|memorize|"
    r"assistant|model|gemini|claude|gpt|openai|vapi|database|sql|script)\b"
    r"|[<>{}\[\]`|\\]|https?:|www\.|\.com\b",
    re.I,
)
# Health conditions / diagnoses / medical framing: never stored (the note is dropped, not rephrased).
# Experiential words ("sleep", "relax", "unwind") are fine; the condition words are not.
_HEALTH = re.compile(
    r"\b(cancer\w*|tumou?r\w*|chemo\w*|oncolog\w*|diabet\w*|epilep\w*|seizure\w*|ptsd|depress\w*|anxiety|"
    r"panic attacks?|bipolar|schizo\w*|adhd|autis\w*|arthrit\w*|fibromyalgia|glaucoma|crohn\w*|"
    r"colitis|ibs|hiv|aids|sclerosis|parkinson\w*|alzheimer\w*|dementia|insomnia|migraine\w*|"
    r"chronic|pain\w*|nausea|vomit\w*|appetite|prescri\w*|medicat\w*|meds|medical\w*|medicin\w*|doctor\w*|"
    r"physician|diagnos\w*|disease\w*|disorder\w*|illness\w*|sick\w*|pregnan\w*|opioid\w*|rehab|"
    r"addict\w*|sober|recovery|conditions?|symptom\w*|therap\w*|treatment\w*|treating|to treat|cure[sd]?\b|curing|heal\w*|patient\w*|"
    r"surgery|injur\w*|disab\w*|cardiac|heart|blood|asthma|copd|lupus|hepatitis|kidney|liver|stroke)\b",
    re.I,
)
# Capitalised words allowed mid-string (abbreviations, our own store names). Any other capitalised
# token after the first word reads as a proper name (someone else's name, a brand we did not vet).
_CAPS_OK = {"CBD", "THC", "CBN", "CBG", "THCV", "OG", "Yakima", "Mount", "Vernon", "Pullman", "Happy", "Time",
            "I", "I'm", "I've", "I'd"}


def clean_text(value: object, limit: int) -> str:
    """One line of plain text: control/format characters removed, whitespace collapsed, capped."""
    text = _CTRL.sub(" ", str(value if value is not None else ""))
    return " ".join(text.split())[:limit].strip()


def quarantined(text: str) -> bool:
    """True when ``text`` must never be stored: PII, an instruction/injection shape, a health
    condition, or what looks like another person's name."""
    if not text:
        return True
    if any(p.search(text) for p in _PII) or _INJECTION.search(text) or _HEALTH.search(text):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    return any(w[0].isupper() and w not in _CAPS_OK and w.upper() != w for w in words[1:])


def _ok_str(value: object, limit: int) -> str:
    text = clean_text(value, limit)
    return "" if quarantined(text) else text


# A summary never carries a price or dollar amount (on top of the quarantine above).
_PRICE = re.compile(r"\$|\b\d+(?:\.\d+)?\s*(?:dollars?|bucks|usd|cents?)\b|\b(?:dollars?|bucks)\b", re.I)


def summary_ok(text: str) -> bool:
    """A conversation summary that may be stored: clears the quarantine and names no price."""
    return bool(text) and not quarantined(text) and not _PRICE.search(text)


def _summary_str(value: object, limit: int) -> str:
    text = clean_text(value, limit)
    return text if summary_ok(text) else ""


def entry_key(text: str) -> str:
    """Stable short id of a summary entry (ChatSession.learned["skey"] points at its own entry)."""
    import hashlib

    return hashlib.sha256(_key(text).encode()).hexdigest()[:16]


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9:]+", " ", text.lower()).split())


# ── derived (computed from purchases by customer_model.compute_derived) ──────
_DERIVED_SHARE_MAPS = ("forms", "extraction")
_DERIVED_BAND_MAPS = ("price_by_cat", "thc_by_cat")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9 :/&+._-]{0,31}$", re.I)
# A pairing is "category|category" (customer_model.compute_derived); the pipe is allowed ONLY here.
_PAIR = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}\|[a-z0-9][a-z0-9-]{0,30}$", re.I)


def _num(v, lo: float, hi: float):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v < lo or v > hi:  # NaN / out of range
        return None
    return round(float(v), 3) if isinstance(v, float) else v


def _labels(v, n: int) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        s = clean_text(x, 32)
        if s and _LABEL.match(s) and s not in out:
            out.append(s)
    return out[:n]


def sanitize_derived(raw: object) -> dict:
    """The contract's ``derived`` keys only, with bounded shapes; anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    if ratio := [r for r in _labels(raw.get("ratio_pref"), 6) if re.fullmatch(r"\d{1,3}:\d{1,3}(:\d{1,3})?", r)]:
        out["ratio_pref"] = ratio
    if (v := _num(raw.get("cbd_lean"), 0, 1)) is not None:
        out["cbd_lean"] = v
    for k in _DERIVED_SHARE_MAPS:
        m = raw.get(k)
        if isinstance(m, dict):
            vals = {clean_text(a, 32): _num(b, 0, 1) for a, b in m.items()}
            vals = {a: b for a, b in vals.items() if a and _LABEL.match(a) and b is not None}
            if vals:
                out[k] = dict(sorted(vals.items(), key=lambda kv: -kv[1])[:8])
    dose = raw.get("dose_mg")
    if isinstance(dose, dict):
        d = {k: _num(dose.get(k), 0, 10000) for k in ("min", "p50", "max")}
        if d := {k: v for k, v in d.items() if v is not None}:
            out["dose_mg"] = d
    for k in _DERIVED_BAND_MAPS:
        m = raw.get(k)
        if isinstance(m, dict):
            bands = {}
            for cat, band in list(m.items())[:12]:
                cat = clean_text(cat, 32)
                if not (cat and _LABEL.match(cat) and isinstance(band, dict)):
                    continue
                b = {p: _num(band.get(p), 0, 100000) for p in ("p10", "p50", "p90")}
                if b := {p: v for p, v in b.items() if v is not None}:
                    bands[cat] = b
            if bands:
                out[k] = bands
    for k in ("cadence_days", "days_since_last"):
        if (v := _num(raw.get(k), 0, 36500)) is not None:
            out[k] = v
    if isinstance(raw.get("due_for_reorder"), bool):
        out["due_for_reorder"] = raw["due_for_reorder"]
    pairs = raw.get("pairings")
    if isinstance(pairs, dict):
        def _pairs(v: object) -> list[str]:
            seen: list[str] = []
            for x in (v if isinstance(v, list) else [])[:16]:
                x = clean_text(x, 62)
                if x and _PAIR.match(x) and x not in seen:
                    seen.append(x)
            return seen[:8]

        p = {k: _pairs(pairs.get(k)) for k in ("accepted", "declined")}
        if p := {k: v for k, v in p.items() if v}:
            out["pairings"] = p
    if nl := _labels(raw.get("next_likely"), 5):
        out["next_likely"] = nl
    if raw.get("confidence") in ("low", "med", "high"):
        out["confidence"] = raw["confidence"]
    return out


# ── schema validation + caps ─────────────────────────────────────────────────
def _style(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, allowed in STYLE_ENUMS.items():
        if raw.get(k) in allowed:
            out[k] = raw[k]
    for k in STYLE_BOOLS:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    return out


def _at(value: object) -> str:
    s = str(value or "")[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return timezone.localdate().isoformat()


def _notes(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for n in reversed(raw):  # newest last: keep the newest copy of a repeated note
        if not isinstance(n, dict):
            continue
        t = _ok_str(n.get("t"), NOTE_CHARS)
        if not t or _key(t) in seen:
            continue
        seen.add(_key(t))
        out.append({"t": t, "at": _at(n.get("at")), "src": n.get("src") if n.get("src") in SOURCES else "chat"})
    return list(reversed(out))[-NOTES_MAX:]


def _summaries(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for n in reversed(raw):  # newest last: keep the newest copy of a repeated entry
        if not isinstance(n, dict):
            continue
        t = _summary_str(n.get("t"), SUMMARY_ENTRY_CHARS)
        if not t or _key(t) in seen:
            continue
        seen.add(_key(t))
        out.append({"t": t, "at": _at(n.get("at")), "src": n.get("src") if n.get("src") in SOURCES else "chat"})
    return list(reversed(out))[-SUMMARIES_MAX:]


def _list(raw: object, n: int, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in reversed(raw):
        s = _ok_str(x, limit)
        if s and _key(s) not in seen:
            seen.add(_key(s))
            out.append(s)
    return list(reversed(out))[-n:]


def _size(mem: dict) -> int:
    return len(json.dumps(mem, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _fit(mem: dict) -> dict:
    """Trim to MAX_BYTES: oldest conversation summaries first, then oldest notes, then the oldest list
    entries, then derived detail; the consolidated ``summary`` goes last."""
    order = ["summaries", "notes", "last_topics", "context", "likes", "dislikes"]
    while _size(mem) > MAX_BYTES:
        for k in order:
            if mem.get(k):
                mem[k].pop(0)
                if not mem[k]:
                    mem.pop(k)
                break
        else:
            d = mem.get("derived") or {}
            for k in ("pairings", "thc_by_cat", "price_by_cat", "extraction", "forms"):
                if k in d:
                    d.pop(k)
                    break
            else:
                if "derived" in mem:
                    mem.pop("derived")
                elif "summary" in mem:
                    mem.pop("summary")
                elif _size(mem) > MAX_BYTES:  # only style/meta left; cannot happen with these caps
                    return {"v": VERSION}
    return mem


def sanitize(raw: object, *, session: bool = False) -> dict:
    """Anything not in the v1 schema is dropped; every string clears the quarantine; caps applied.
    ``session=True`` is the ChatSession.learned variant: no ``derived``, plus idempotency meta."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {"v": VERSION}
    if style := _style(raw.get("style")):
        out["style"] = style
    if notes := _notes(raw.get("notes")):
        out["notes"] = notes
    for k, (n, limit) in LISTS.items():
        if vals := _list(raw.get(k), n, limit):
            out[k] = vals
    if summaries := _summaries(raw.get("summaries")):
        out["summaries"] = summaries
    if not session and (summary := _summary_str(raw.get("summary"), SUMMARY_CHARS)):
        out["summary"] = summary
    if session:
        for k in ("digest", "ldigest", "sdigest"):
            if isinstance(raw.get(k), str) and re.fullmatch(r"[0-9a-f]{8,64}", raw[k]):
                out[k] = raw[k]
        if isinstance(raw.get("skey"), str) and re.fullmatch(r"[0-9a-f]{16}", raw["skey"]):
            out["skey"] = raw["skey"]
        if isinstance(raw.get("upto"), int) and not isinstance(raw.get("upto"), bool) and raw["upto"] >= 0:
            out["upto"] = raw["upto"]
    elif derived := sanitize_derived(raw.get("derived")):
        out["derived"] = derived
    return _fit(out)


def merge(existing: object, learned: object, *, session: bool = False) -> dict:
    """Fold freshly learned facts into stored memory.

    Style: a newer measurement replaces the older one, key by key. likes/dislikes: the newer
    statement about the same thing replaces the older one (a new "like X" drops an old "dislike X"
    and vice versa); within one batch a dislike beats a like. Notes/context/topics: de-duplicated
    (a repeat moves to the end, it is never stored twice), newest last, capped. ``derived`` is never
    taken from ``learned`` (it comes from purchases only)."""
    base = sanitize(existing, session=session)
    new = sanitize(learned, session=True)
    out = dict(base)
    if new.get("style"):
        out["style"] = {**base.get("style", {}), **new["style"]}
    dis_new = new.get("dislikes", [])
    dis_keys = {_key(x) for x in dis_new}
    likes_new = [x for x in new.get("likes", []) if _key(x) not in dis_keys]
    touched = dis_keys | {_key(x) for x in likes_new}
    out["likes"] = [x for x in base.get("likes", []) if _key(x) not in touched] + likes_new
    out["dislikes"] = [x for x in base.get("dislikes", []) if _key(x) not in touched] + dis_new
    for k in ("context", "last_topics"):
        fresh = new.get(k, [])
        fk = {_key(x) for x in fresh}
        out[k] = [x for x in base.get(k, []) if _key(x) not in fk] + fresh
    nk = {_key(n["t"]) for n in new.get("notes", [])}
    out["notes"] = [n for n in base.get("notes", []) if _key(n["t"]) not in nk] + new.get("notes", [])
    # Conversation summaries normally arrive through memory_summary, never through ``learned``; a
    # profile merge (identity._merge) folds the shell row's in: its entries are appended, and its
    # consolidated summary becomes an entry when this row already has one (the next consolidation
    # folds it), so nothing the same person said is lost.
    extra = list(new.get("summaries", []))
    other = _summary_str(learned.get("summary") if isinstance(learned, dict) else "", SUMMARY_CHARS)
    if other and not session:
        if base.get("summary"):
            extra.insert(0, {"t": clean_text(other, SUMMARY_ENTRY_CHARS), "at": timezone.localdate().isoformat(),
                             "src": "voice"})
        else:
            out["summary"] = other
    if extra:
        sk = {_key(n["t"]) for n in extra}
        out["summaries"] = [n for n in base.get("summaries", []) if _key(n["t"]) not in sk] + extra
    if session:
        for k in SESSION_META:
            if k in new:
                out[k] = new[k]
    return sanitize(out, session=session)


def set_derived(profile, derived: object) -> bool:
    """Store ``derived`` (from customer_model.compute_derived) under a row lock, leaving the learned
    facts alone. Never raises."""
    from django.db import transaction

    from .models import CustomerProfile

    try:
        clean = sanitize_derived(derived)
        with transaction.atomic():
            row = CustomerProfile.objects.select_for_update().get(pk=profile.pk)
            mem = sanitize(row.memory)
            if clean:
                mem["derived"] = clean
            else:
                mem.pop("derived", None)
            mem = sanitize(mem)
            if mem != (row.memory or {}):
                CustomerProfile.objects.filter(pk=row.pk).update(memory=mem)
        profile.memory = mem
        return True
    except Exception:  # noqa: BLE001 - derived is a nicety; a failure must not break the recompute
        logger.warning("memory: could not store derived for profile %s", getattr(profile, "pk", "?"), exc_info=True)
        return False


# ── the brief ────────────────────────────────────────────────────────────────
_EMPTY = {"text": "", "style": {}, "public": True}


def style_line(style: dict) -> str:
    bits = [style[k] for k in ("length", "tone") if style.get(k)]
    if "emoji" in style:
        bits.append("emoji ok" if style["emoji"] else "no emoji")
    if style.get("pace") == "quick":
        bits.append("likes quick picks")
    elif style.get("pace") == "browse":
        bits.append("likes to browse options")
    if style.get("wants_explanations"):
        bits.append("likes the why")
    return f"Style: {', '.join(bits)}." if bits else ""


def _top(weights: object, n: int) -> list[str]:
    if not isinstance(weights, dict):
        return []
    items = [(clean_text(k, 32), v) for k, v in weights.items() if isinstance(v, (int, float))]
    return [k for k, _ in sorted(items, key=lambda kv: -kv[1]) if k][:n]


def _cadence(days) -> str:
    if not isinstance(days, (int, float)) or days <= 0:
        return ""
    if days < 10:
        return f"~every {max(1, round(days))}d"
    if days < 60:
        return f"~every {max(1, round(days / 7))} wks"
    return f"~every {max(1, round(days / 30))} mo"


_PRICE_WORD = {"value": "value price", "mid": "mid price", "top": "top shelf"}


def _usually(profile, derived: dict) -> str:
    bits: list[str] = []
    ratios = derived.get("ratio_pref") or []
    forms = _top(derived.get("forms"), 2)
    dose = (derived.get("dose_mg") or {}).get("p50")
    lead = " and ".join(ratios[:2])
    if forms:
        lead = f"{lead} {' and '.join(forms)}".strip()
    elif not lead:
        lead = ", ".join(_top(profile.category_affinity, 2))
    if lead and dose:
        lead = f"{lead} {dose:g}mg"
    if lead:
        bits.append(lead)
    if ext := _top(derived.get("extraction"), 2):
        bits.append(" and ".join(ext))
    if brands := _top(profile.brand_affinity, 2):
        bits.append("brands " + ", ".join(brands))
    if flav := (_top(profile.terpene_affinity, 1) + _top(profile.flavor_affinity, 1)):
        bits.append(", ".join(dict.fromkeys(flav)))
    if word := _PRICE_WORD.get(profile.price_tier or ""):
        bits.append(word)
    return f"Usually buys: {'; '.join(bits)}." if bits else ""


def _last_bought(profile) -> str:
    best, when = None, None
    for h in profile.purchase_history or []:
        if not isinstance(h, dict) or not h.get("last_bought_at"):
            continue
        try:
            dt = datetime.fromisoformat(str(h["last_bought_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        if when is None or dt > when:
            best, when = h, dt
    name = clean_text((best or {}).get("product_name"), 48)
    if not name or _INJECTION.search(name):
        return ""
    days = max(0, (timezone.now() - when).days)
    return f"Last: {name} ({days}d ago)."


def brief(profile, tier: str) -> dict:
    """``{"text", "style", "public"}`` for this profile at this trust tier; text <= 600 chars.

    trusted    name, cadence, style, usual buys + last purchase, likes/avoids, what they said, topics,
               and a last ``Remembers:`` line (consolidated summary + up to 2 newest entries, cut to fit)
    unverified (``public``) style + purchase-backed taste only; a name only when purchases back the
               row (identity.context's website rule); no notes/likes/dislikes/context/topics/last buy;
               the ``Remembers:`` line only while HHT_MEMORY_WEB_SUMMARIES is on (default off)
    anonymous  nothing"""
    from . import identity

    if profile is None or tier not in (TRUSTED, UNVERIFIED) or identity.shared_profile(profile):
        return dict(_EMPTY)
    public = tier != TRUSTED
    mem = sanitize(profile.memory)
    style = mem.get("style", {})
    derived = mem.get("derived", {})
    orders = int(profile.total_orders or 0)

    head: list[str] = []
    name = identity.first_name(profile.name)
    if name and (not public or orders > 0):
        head.append(f"Name: {name}")
    status = "returning" if orders > 0 else "new"
    if cad := _cadence(derived.get("cadence_days")):
        status = f"{status}, {cad}"
    head = [f"{head[0]} ({status})." if head else f"Customer: {status}."]
    if s := style_line(style):
        head.append(s)
    lines = [" ".join(head)]

    buys = [x for x in (_usually(profile, derived),) if x]
    if derived.get("due_for_reorder"):
        buys.append("Due for a reorder.")
    if nl := derived.get("next_likely"):
        buys.append(f"Next likely: {', '.join(nl[:2])}.")
    if not public and (last := _last_bought(profile)):
        buys.append(last)
    if buys:
        lines.append(" ".join(buys))

    if not public:
        said = [*mem.get("context", []), *(n["t"] for n in reversed(mem.get("notes", [])))]
        pieces = [p for p in (
            f"Likes: {', '.join(mem['likes'])}." if mem.get("likes") else "",
            f"Avoids: {', '.join(mem['dislikes'])}." if mem.get("dislikes") else "",
            f"Said: {'; '.join(said)}." if said else "",
            f"Recent topics: {', '.join(mem['last_topics'])}." if mem.get("last_topics") else "",
        ) if p]
        if pieces:
            lines.append(" ".join(pieces))

    text = "\n".join(clean_text(line, BRIEF_MAX) for line in lines if line.strip())
    if len(text) > BRIEF_MAX:
        cut = text[:BRIEF_MAX - 1]
        cut = cut[:max(cut.rfind(" "), cut.rfind("\n"))] if (" " in cut or "\n" in cut) else cut
        text = cut.rstrip(" ,;:") + "…"
    # Conversation summaries: trusted always; the unverified (typed website phone) tier only when the
    # owner sets HHT_MEMORY_WEB_SUMMARIES (a typed number is not proof of identity).
    if not public or web_summaries_enabled():
        if line := remembers_line(mem, BRIEF_MAX - len(text) - (1 if text else 0)):
            text = f"{text}\n{line}" if text else line
    return {"text": text[:BRIEF_MAX], "style": style, "public": public}


def web_summaries_enabled() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "HHT_MEMORY_WEB_SUMMARIES", False))


def _sentence(text: str) -> str:
    text = text.strip().rstrip(" ,;:")
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


def remembers_line(mem: dict, budget: int) -> str:
    """``Remembers: <consolidated summary> <up to 2 newest entries>`` within ``budget`` characters.
    Entries are cut first (oldest of the two, then the other); the consolidated summary is cut at a
    word only when it alone does not fit; "" when nothing fits (the brief cap is never exceeded)."""
    label = "Remembers: "
    if budget <= len(label) + 12:
        return ""
    summary = mem.get("summary") or ""
    entries = [e["t"] for e in (mem.get("summaries") or [])[-2:]]
    for keep in range(len(entries), -1, -1):
        parts = ([_sentence(summary)] if summary else []) + [_sentence(t) for t in entries[len(entries) - keep:]]
        if not parts:
            return ""
        line = label + " ".join(parts)
        if len(line) <= budget:
            return line
    room = budget - len(label) - 1
    cut = (summary or entries[-1])[:room]
    cut = cut[:cut.rfind(" ")] if " " in cut else ""
    return f"{label}{cut.rstrip(' ,;:')}…" if len(cut) >= 12 else ""


def _shingles(text: str, n: int = 8) -> set[str]:
    words = _key(text).split()
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def echoes(reply: str, mem: object) -> bool:
    """True when ``reply`` recites stored memory: a conversation summary, the consolidated summary or a
    note, verbatim or as a run of 8+ of its words. The bot personalises silently; it never reads the
    customer's memory back to them (owner rule), so a reply that does is replaced, not shown."""
    mem = mem if isinstance(mem, dict) else {}
    secrets = [mem.get("summary") or ""] + [
        e.get("t", "") for k in ("summaries", "notes") for e in (mem.get(k) or []) if isinstance(e, dict)]
    said = _key(str(reply or ""))
    if not said:
        return False
    said_sh = _shingles(said)
    for s in secrets:
        k = _key(str(s or ""))
        if not k:
            continue
        if (len(k) >= 24 and k in said) or (_shingles(k) & said_sh):
            return True
    return False
