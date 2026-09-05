"""The golden answer set: load, validate, and resolve fact templates against the KB.

Template grammar (inside ``must_include`` / ``must_not_include``)::

    {{storefact:<store>:<kind>[:<label substring>]}}
                                      kb.StoreFact(store, kind).value   ("" store = global)
    {{limit:<term>}}                  the WA purchase-limit StoreFact whose label mentions term
    {{deal:<store>:<label substring>}}
                                      the CURRENT (in its valid_from/valid_to window) special for
                                      that store; when none is running today it resolves to the
                                      "no specials posted" wording instead — the right answer
                                      depends on the date, so the golden file can't hardcode it
    {{faq:<key>}}                     kb.FAQEntry(key).answer
    {{policy:<slug>}}                 kb.PolicyDocument(category.slug).body
    {{safety:<name>}}                 the owner-signed safety strings in voice.chat
    {{greeting}}                      the spoken opener (AgentPrompt entry_router.first_message)
    {{faq_fallback}}                  voice.tools.faq._FALLBACK
    {{other_months}}                  a regex of every month name EXCEPT the current one — a
                                      deal answer naming one of them is stale

An optional ``|filter`` extracts the comparable *atoms* of a value instead of the whole string,
so two channels that phrase the same fact differently still agree on the fact::

    |times     every "8 AM–11:30 PM" style range          → each must appear
    |phone     the 10 digits of a phone number            → must appear (any punctuation)
    |numbers   every number (+ its unit word: "7 grams", "30% off")   → each must appear
    |first     the first sentence, as a loose regex
    |address   house number, street name, city, zip   → each must appear, any order

A pattern with no ``{{…}}`` is a plain regex (case-insensitive) — for tone/safety phrases only.
A guard test refuses literal hours/prices in the golden file (``test_eval_answers.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "kb" / "golden" / "answers.yaml"

CATEGORIES = (
    "store-facts", "pricing-tax", "age-id", "policies", "deals", "products",
    "safety", "escalation", "vendor", "out-of-scope", "adversarial",
)
CHANNELS = ("text", "playground", "voice", "web", "web-fallback", "pos", "storefront", "sms")

_TEMPLATE = re.compile(r"\{\{\s*([a-z_]+)(?::([^|}]*))?\s*(?:\|\s*([a-z_]+))?\s*\}\}")
_TIME_RANGE = re.compile(
    r"\d{1,2}(?::\d{2})?\s*(?:AM|PM)\s*[–—-]\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM)", re.I
)
_PHONE_SHAPE = re.compile(r"(?:\+?1[\s.,-]*)?\(?\d{3}\)?[\s.,-]*\d{3}[\s.,-]*\d{4}(?!\d)")  # spoken groups may be comma-separated
_NUMBER = re.compile(r"\$?\d+(?:[.,]\d+)?%?(?:\s*[a-z]+)?", re.I)
# What every channel must say when no special is running today (voice/tools/faq.py speaks it).
NO_DEALS_RE = r"(?i)(no specials (are )?posted|don'?t have any specials|no deals (are )?(running|posted))"
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


@dataclass
class Entry:
    id: str
    category: str
    question_variants: list[str]
    store: str = ""
    must_include: list[str] = field(default_factory=list)
    must_not_include: list[str] = field(default_factory=list)
    tone: str = "spoken_and_written"
    # Spoken replies legitimately end with one follow-up question ("how do you want to feel?"),
    # which the written channel doesn't do — hence the larger spoken default than a bare fact needs.
    max_words: dict = field(default_factory=lambda: {"spoken": 80, "written": 110})
    channels: list[str] = field(default_factory=lambda: ["text", "playground", "voice", "web"])
    source_of_truth: str = ""
    expect_intent: str = ""
    expect_tools: list[str] = field(default_factory=list)
    expect_grounded: bool | None = None
    expect_escalated: bool | None = None
    setup_turns: list[str] = field(default_factory=list)  # prior messages in the same session
    phone: str = ""
    owner_note: str = ""

    @property
    def question(self) -> str:
        return self.question_variants[0]


def load(path: Path | None = None) -> list[Entry]:
    raw = yaml.safe_load((path or GOLDEN_PATH).read_text(encoding="utf-8")) or []
    entries = [Entry(**row) for row in raw]
    _validate(entries)
    return entries


def _validate(entries: list[Entry]) -> None:
    seen = set()
    for e in entries:
        if e.id in seen:
            raise ValueError(f"duplicate golden id {e.id}")
        seen.add(e.id)
        if e.category not in CATEGORIES:
            raise ValueError(f"{e.id}: unknown category {e.category}")
        for ch in e.channels:
            if ch not in CHANNELS:
                raise ValueError(f"{e.id}: unknown channel {ch}")
        if not e.question_variants:
            raise ValueError(f"{e.id}: needs at least one question")


# ── normalisation shared by matching and consistency ─────────────────────────

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SPOKEN_NUM = re.compile(
    r"\b(?:(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-](one|two|three|four|five|six|seven|eight|nine))?"
    r"|(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen))\b",
    re.I,
)


def spoken_to_digits(text: str) -> str:
    """The voice channel speaks numbers as words (SPEAKING_RULES): "eight A M to eleven thirty
    P M", "thirty percent off", "twenty-two dollars". Fold those back to the written form so one
    fact matcher serves both channels."""

    def _num(m):
        tens, unit, single = m.group(1), m.group(2), m.group(3)
        if single:
            return str(_UNITS[single.lower()])
        return str(_TENS[tens.lower()] + (_UNITS[unit.lower()] if unit else 0))

    # Spelled-out digit strings: "one-three-one-five" / "five oh nine" → 1315 / 509.
    def _digits(m):
        words = [w for w in re.split(r"[\s-]+", m.group(0).strip()) if w]
        return "".join("0" if w.lower() == "oh" else str(_UNITS.get(w.lower(), 0)) for w in words)

    # "double eight" / "triple five" → "eight eight" / "five five five" before the run is folded.
    t = re.sub(r"\b(double|triple) (zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
               lambda m: " ".join([m.group(2)] * (2 if m.group(1).lower() == "double" else 3)),
               text, flags=re.I)
    t = re.sub(
        r"\b(?:(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)(?:[\s-]+(?=(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b)|(?![a-z]))){2,}",
        lambda m: _digits(m), t, flags=re.I,
    )
    t = _SPOKEN_NUM.sub(_num, t)
    t = re.sub(r"\b([ap])\s*\.?\s*m\b\.?", r"\1m", t, flags=re.I)          # "A M" / "p.m." → am/pm
    t = re.sub(r"\b([A-Z]) (?=[A-Z]\b)", r"\1", t)                        # "I D" / "T H C" → ID / THC
    # Spoken street forms → the written KB form ("North First Street" → "N 1st St").
    for spoken, written in (("north", "n"), ("south", "s"), ("east", "e"), ("west", "w"),
                            ("first", "1st"), ("second", "2nd"), ("third", "3rd"),
                            ("street", "st"), ("avenue", "ave"), ("lane", "ln"), ("road", "rd"),
                            ("highway", "wa-"), ("washington", "wa")):
        t = re.sub(rf"\b{spoken}\b\.?", written, t, flags=re.I)
    t = re.sub(r"\b(\d{1,2}) (\d{2})\s*(am|pm)\b", r"\1:\2\3", t, flags=re.I)  # "11 30 pm" → 11:30pm
    t = re.sub(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)) to (\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", r"\1-\2", t, flags=re.I)
    t = re.sub(r"\b(\d+) percent\b", r"\1%", t, flags=re.I)
    t = re.sub(r"\b(\d+) dollars?\b", r"$\1", t, flags=re.I)
    return t


def norm(text: str) -> str:
    """Whitespace-collapsed, dash-unified, lowercase, spoken numbers folded to digits — the form
    every comparison runs on."""
    t = spoken_to_digits(str(text or ""))
    t = t.replace("–", "-").replace("—", "-").replace("‑", "-")
    t = re.sub(r"\s+", " ", t).strip().lower()
    t = re.sub(r"\s*-\s*", "-", t)          # "8 AM - 11 PM" == "8 AM-11 PM"
    t = re.sub(r"(\d)\s+(am|pm)\b", r"\1\2", t)  # "8 am" == "8am"
    return t


def atoms(value: str, flt: str) -> list[str]:
    """The comparable pieces of a fact value under a filter (see module docstring)."""
    v = str(value or "")
    if flt == "times":
        return [norm(m) for m in _TIME_RANGE.findall(norm(v))]  # norm first: spoken → digits
    if flt == "phone":
        m = _PHONE_SHAPE.search(norm(v))  # norm first: "five oh nine, …" → digits
        return [re.sub(r"\D", "", m.group(0))[-10:]] if m else []
    if flt == "numbers":
        return [norm(m) for m in _NUMBER.findall(v)]
    if flt == "address":
        # "1315 N 1st St, Yakima, WA 98901" → the pieces a spoken address must carry, in any
        # order and with any filler ("thirteen fifteen North First Street, in Yakima, … zip is").
        n = norm(v)
        out = []
        m = re.match(r"(\d+)\s+(.+?),\s*([^,]+),\s*wa\s*(\d{5})", n)
        if m:
            house, street, city, zipc = m.groups()
            out = [house, street.split()[0], city.strip(), zipc]
        return out or [n]
    if flt == "first":
        first = re.split(r"(?<=[.!?])\s", v.strip(), maxsplit=1)[0]
        return [norm(first).rstrip(".!?")]
    return [norm(v)]


ANY_OF_FILTERS = ("numbers",)  # "1 ounce (28 g)" — either form of the figure satisfies the fact


def _loose_regex(atom: str) -> re.Pattern:
    """Match a normalised atom inside a normalised answer, tolerant of punctuation drift."""
    parts = [re.escape(p) for p in re.split(r"[\s]+", atom) if p]
    return re.compile(r"[\s\W]*".join(parts), re.I)


def _phone_regex(digits: str) -> re.Pattern:
    return re.compile(r"\D*".join(digits))


# ── resolution against the KB ────────────────────────────────────────────────

def _fact_value(kind: str, arg: str) -> str:
    from kb import models as m

    if kind == "storefact":
        bits = [b.strip() for b in (arg or "").split(":")]
        store, fact_kind = bits[0], (bits[1] if len(bits) > 1 else "")
        label_sub = bits[2] if len(bits) > 2 else ""
        qs = m.StoreFact.objects.filter(store=store, kind=fact_kind, is_active=True)
        if label_sub:
            qs = qs.filter(label__icontains=label_sub)
        row = qs.order_by("-weight", "label").first()
        if row is None:
            raise LookupError(f"no StoreFact(store={store!r}, kind={fact_kind!r}, label~{label_sub!r})")
        return row.value
    if kind == "deal":
        import datetime as _dt

        store, _, label_sub = (arg or "").partition(":")
        qs = m.StoreFact.objects.filter(store=store.strip(), kind="special", is_active=True)
        if hasattr(m.StoreFact.objects, "current"):
            qs = m.StoreFact.objects.current(_dt.date.today()).filter(
                store=store.strip(), kind="special", is_active=True
            )
        if label_sub.strip():
            qs = qs.filter(label__icontains=label_sub.strip())
        row = qs.order_by("-weight", "label").first()
        if row is not None:
            return row.value
        return NO_DEALS_RE
    if kind == "limit":
        row = m.StoreFact.objects.filter(
            store="", kind="limit", label__icontains=arg.strip(), is_active=True
        ).first()
        if row is None:
            raise LookupError(f"no WA limit StoreFact for {arg!r}")
        return row.value
    if kind == "other_months":
        import datetime as _dt

        now = _dt.date.today().strftime("%B").lower()
        return r"\b(" + "|".join(mo for mo in _MONTHS if mo != now) + r")\b"
    if kind == "faq":
        row = m.FAQEntry.objects.filter(key=arg.strip(), is_active=True).first()
        if row is None:
            raise LookupError(f"no FAQEntry(key={arg!r})")
        return row.answer
    if kind == "policy":
        row = m.PolicyDocument.objects.filter(category__slug=arg.strip(), is_active=True).first()
        if row is None:
            raise LookupError(f"no PolicyDocument(category={arg!r})")
        return row.body
    if kind == "safety":
        from voice import chat

        fn = {
            "escalation": chat._escalation_answer,
            "cannot_answer": chat._cannot_answer_safely_answer,
            "poison": chat._poison_emergency_answer,
        }[arg.strip()]
        # The follow-up hint varies by store/phone; the owner-signed sentence is the fixed part.
        return fn("", "").split(" Please share")[0]
    if kind == "greeting":
        from voice.provision import entry_greeting

        return entry_greeting()
    if kind == "faq_fallback":
        from voice.tools import faq

        return faq._FALLBACK
    raise LookupError(f"unknown template kind {kind}")


@dataclass
class Check:
    """One resolved must/must-not rule: the regexes that must (all) hit, and what they came from."""

    source: str
    regexes: list[re.Pattern]
    atoms: list[str]
    literal: bool  # True = plain regex written in the golden file (no template)
    any_of: bool = False  # True = one regex hitting is enough (see ANY_OF_FILTERS)

    def hits(self, text: str) -> bool:
        found = [bool(rx.search(text)) for rx in self.regexes]
        return any(found) if self.any_of else all(found)


def resolve(pattern: str) -> Check:
    m = _TEMPLATE.fullmatch(pattern.strip())
    if not m:
        return Check(source=pattern, regexes=[re.compile(pattern, re.I)], atoms=[], literal=True)
    kind, arg, flt = m.group(1), (m.group(2) or ""), (m.group(3) or "")
    value = _fact_value(kind, arg)
    if kind == "other_months" or value == NO_DEALS_RE:  # already a regex, not a fact value
        return Check(source=pattern, regexes=[re.compile(value, re.I)], atoms=[], literal=False)
    found = atoms(value, flt)
    if not found:
        raise LookupError(f"{pattern}: filter {flt!r} extracted nothing from {value!r}")
    if flt == "phone":
        regexes = [_phone_regex(a) for a in found]
    elif flt == "address":
        # digits may be spoken in groups ("thirteen fifteen" → "13 15"), so allow gaps inside them
        regexes = [re.compile(r"\s?".join(a) if a.isdigit() else r"\b" + re.escape(a) + r"\b", re.I)
                   for a in found]
    else:
        regexes = [_loose_regex(a) for a in found]
    return Check(source=pattern, regexes=regexes, atoms=found, literal=False,
                 any_of=flt in ANY_OF_FILTERS)
