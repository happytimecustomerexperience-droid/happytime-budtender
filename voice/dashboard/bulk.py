"""Bulk data layer for the voice dashboard: one registry of editable datasets, a CSV round trip, and
the shared validate/apply engine behind inline editing, "Edit all", row actions and CSV upload.

Everything validates through the dataset's existing ModelForm (``dashboard.forms.KB_FORMS`` etc.), so
a bulk path can never accept a row the single-row editor would refuse (prompt-injection screen,
unique keys, date order, ...). This module has no HTTP in it; ``dashboard/bulk_views.py`` is the
thin layer on top.

CSV contract (documented on the upload page too):

* Export/template are UTF-8 with a BOM (Excel opens them correctly), CRLF rows, no ``#`` comment lines.
* Upload accepts UTF-8, UTF-8 with BOM, UTF-16 (BOM or not) and Windows-1252; comma, tab or
  semicolon separated. Headers are trimmed and case-insensitive (``Valid From`` = ``valid_from``).
* Rows upsert on the dataset's natural key. A row missing from the file is NEVER deleted; a row is
  deleted only when its ``delete`` cell says yes AND "Allow deletes" was ticked.
* A column that is absent leaves that field alone (new rows get the model default). A column that is
  present but blank clears a text/date field; for yes/no and whole-number fields blank means "keep
  the current value (or the default for a new row)".
* Formula safety. Export: a cell starting with ``=  +  -  @``, tab or CR gets a leading apostrophe.
  Import: that one apostrophe is stripped again (so export -> import is lossless); a cell that starts
  with ``=`` or ``@``, or with ``+``/``-`` and is not a plain number/phone, is REJECTED with a row
  error unless the author typed the apostrophe themselves. File contents are never logged.
* Limits: 1 MB, 2,000 data rows. Row numbers in errors are spreadsheet rows (header = row 1).

Side effects (the StoreFact -> budtender "store-facts" nudge) run ONCE per batch through
``run_side_effects``; per-row signals are held back for the duration of the write. KB retrieval is
content-hash cached (kb/semantic.py) so it follows the new rows by itself, and the Vapi files mirror
stays the explicit KB "Reindex" button -- exactly as for a single-row save.
"""

from __future__ import annotations

import codecs
import csv
import datetime
import difflib
import hashlib
import io
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from django import forms
from django.db import DatabaseError, transaction
from django.db.models import ProtectedError, Q

from dashboard.forms import (
    KB_FORMS,
    BlogDocForm,
    EducationDocForm,
    FAQEntryForm,
    PolicyCategoryForm,
    PolicyForm,
    StoreFactForm,
    TaxonomyForm,
    VendorAllowlistEntryForm,
)
from dashboard.models import VendorAllowlistEntry
from kb.models import (
    BlogDoc,
    EducationDoc,
    FAQEntry,
    PolicyCategory,
    PolicyDocument,
    StoreFact,
    WeightTypeTaxonomy,
)

logger = logging.getLogger(__name__)

MAX_BYTES = 1_048_576  # 1 MB
MAX_ROWS = 2_000
DELETE_COL = "delete"
STORES = ("yakima", "mount-vernon", "pullman")
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_NUMBERISH = re.compile(r"[+-]?\d[\d\s().\-]*")
_YES = {"yes", "y", "true", "t", "1", "on", "x"}
_NO = {"no", "n", "false", "f", "0", "off"}


class BulkError(Exception):
    """A whole-file problem (too big, wrong encoding, not a CSV) -- nothing is read or written."""


# ── columns + datasets ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Column:
    name: str
    label: str
    type: str  # text | longtext | int | bool | date | choice | url
    required: bool
    max_length: int | None
    choices: tuple[str, ...]
    help: str
    example: str

    @property
    def type_label(self) -> str:
        return {
            "text": "text", "longtext": "long text", "int": "whole number", "bool": "yes / no",
            "date": "date YYYY-MM-DD", "choice": "one of the allowed values", "url": "web address",
        }[self.type]


def _field_type(f: forms.Field) -> str:
    if isinstance(f, forms.BooleanField):
        return "bool"
    if isinstance(f, forms.IntegerField):
        return "int"
    if isinstance(f, forms.DateField):
        return "date"
    if isinstance(f, forms.ChoiceField):
        return "choice"
    if isinstance(f, forms.URLField):
        return "url"
    if isinstance(getattr(f, "widget", None), forms.Textarea):
        return "longtext"
    return "text"


@dataclass
class Dataset:
    key: str
    label: str
    form_cls: type
    model: type
    key_fields: tuple[str, ...] = ()
    fixed: dict = field(default_factory=dict)  # forced on every row AND filters the queryset
    qs_filter: dict = field(default_factory=dict)  # extra queryset filter without forcing a value
    examples: tuple[dict, ...] = ()
    help: dict = field(default_factory=dict)
    choice_override: dict = field(default_factory=dict)  # column -> allowed values
    list_fields: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    list_url: str = ""  # url name of the page that lists this dataset
    active_field: str = ""
    row_template: str = "dashboard/_data_row.html"
    paginate: bool = False
    csv: bool = True  # False: inline/bulk edit only (no natural key, no CSV routes)
    stores_strict: bool = False  # the ``store`` column must be "" or one of STORES
    notify_store_facts: bool = False
    kb_kind: str = ""  # slug of the old standalone KB editor (dashboard.forms.KB_KINDS)
    old_edit_name: str = ""
    old_delete_name: str = ""
    new_initial: dict = field(default_factory=dict)
    ordering: tuple[str, ...] = ()
    can_delete: Callable | None = None  # obj -> error message or ""
    phone_key: bool = False  # ``phone`` is the natural key (normalised to E.164)

    # -- shape ---------------------------------------------------------------------------------
    def queryset(self):
        qs = self.model.objects.all()
        if self.fixed:
            qs = qs.filter(**self.fixed)
        if self.qs_filter:
            qs = qs.filter(**self.qs_filter)
        return qs.order_by(*self.ordering) if self.ordering else qs

    def form_fields(self) -> list[str]:
        return [n for n in self.form_cls.base_fields if n not in self.fixed]

    def columns(self) -> list[Column]:
        form = self.form_cls()
        example = self.examples[0] if self.examples else {}
        out = []
        for name in self.form_fields():
            f = form.fields[name]
            typ = _field_type(f)
            if name in self.choice_override:
                choices = tuple(self.choice_override[name])
                typ = "choice"
            elif name == "store" and self.stores_strict:
                choices = STORES
                typ = "choice"
            elif typ == "choice":
                choices = tuple(str(v) for v, _l in f.choices if str(v) != "")
            else:
                choices = ()
            out.append(
                Column(
                    name=name,
                    label=str(f.label or name),
                    type=typ,
                    required=bool(f.required),
                    max_length=getattr(f, "max_length", None),
                    choices=choices,
                    help=str(self.help.get(name) or f.help_text or ""),
                    example=str(example.get(name, "")),
                )
            )
        return out

    def file_columns(self) -> list[str]:
        return [*self.form_fields(), DELETE_COL]

    @property
    def colspan(self) -> int:
        return len(self.headers) + 2

    def cells(self, obj) -> list[tuple[str, str]]:
        """``(text, badge colour or '')`` per ``list_fields`` for the generic row partial."""
        out = []
        for name in self.list_fields:
            v = getattr(obj, name, "")
            display = getattr(obj, f"get_{name}_display", None)
            if isinstance(v, bool):
                out.append(("yes" if v else "no", "green" if v else "slate"))
            elif callable(display):
                out.append((str(display()), ""))
            else:
                text = str(v if v is not None else "")
                out.append((text if len(text) <= 60 else text[:59] + "…", ""))
        return out

    # -- csv out -------------------------------------------------------------------------------
    def to_cell(self, obj, name: str) -> str:
        if name == "phone" and self.phone_key:
            return obj.phone_display  # "(509) 555-1212": no leading "+", imports back to E.164
        return fmt_value(getattr(obj, name, ""))

    def template_csv(self) -> bytes:
        rows = [[ex.get(n, "") for n in self.form_fields()] + [""] for ex in self.examples]
        return _write_csv(self.file_columns(), rows)

    def export_csv(self, qs=None) -> bytes:
        qs = self.queryset() if qs is None else qs
        names = self.form_fields()
        rows = [[self.to_cell(o, n) for n in names] + [""] for o in qs]
        return _write_csv(self.file_columns(), rows)


def fmt_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, datetime.date):
        return v.isoformat()
    if hasattr(v, "pk"):
        return str(v)
    return str(v)


def neutralise(cell: str) -> str:
    """Export-side formula guard: a leading apostrophe makes a spreadsheet read the cell as text."""
    return "'" + cell if cell[:1] in _FORMULA_LEAD else cell


def _write_csv(header: list[str], rows: list[list[str]]) -> bytes:
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([neutralise(str(c)) for c in r])
    return out.getvalue().encode("utf-8-sig")


def search_queryset(model, qs, q: str):
    q = (q or "").strip()
    if not q:
        return qs
    cond = Q()
    for f in model._meta.fields:
        if f.get_internal_type() in ("TextField", "CharField", "SlugField"):
            cond |= Q(**{f"{f.name}__icontains": q})
    return qs.filter(cond)


def _protected_category(obj) -> str:
    if obj.documents.exists():
        return "still has policy documents under it; move or delete those first"
    return ""


_STORE_HELP = "Blank = every store, otherwise yakima, mount-vernon or pullman (typed exactly)."
_ACTIVE_HELP = "yes = the agent may use this row; no = kept but never spoken."

DATASETS: dict[str, Dataset] = {}


def _register(ds: Dataset) -> Dataset:
    DATASETS[ds.key] = ds
    return ds


_SF_HEADERS = ("Kind", "Store", "Label", "Value", "Runs", "Status")
_SF_COMMON = {
    "kb_kind": "store-fact",
    "old_edit_name": "dash-kb-row-edit",
    "old_delete_name": "dash-kb-row-delete",
    "notify_store_facts": True,
    "stores_strict": True,
    "active_field": "is_active",
    "row_template": "dashboard/_data_row_storefact.html",
    "headers": _SF_HEADERS,
    "ordering": ("kind", "store", "label"),
}
_SF_HELP = {
    "store": _STORE_HELP,
    "label": "Short name of the row. With the store (and kind) it identifies the row: re-uploading "
    "the same label updates that row.",
    "value": "Exactly what the agent may say. Write the % and the products here; the agent never "
    "invents a number.",
    "confirmed": "no = the agent says 'call the store to confirm' instead of speaking the value.",
    "valid_from": "Optional first day the row is spoken (YYYY-MM-DD).",
    "valid_to": "Optional last day the row is spoken (YYYY-MM-DD).",
    "is_active": _ACTIVE_HELP,
}


def _sf_example(kind: str, store: str, label: str, value: str, **extra) -> dict:
    row = {"store": store, "label": label, "value": value, "source_url": "", "confirmed": "yes",
           "valid_from": "", "valid_to": "", "weight": "110", "is_active": "no"}
    if kind:
        row["kind"] = kind
    row.update(extra)
    return row


_register(Dataset(
    key="specials", label="Weekly specials", form_cls=StoreFactForm, model=StoreFact,
    key_fields=("store", "label"), fixed={"kind": "special"}, help=_SF_HELP,
    examples=(
        _sf_example("", "yakima", "Example: Monday flower deal", "20% off all flower on Mondays.",
                    valid_from="2026-11-01", valid_to="2026-11-30"),
        _sf_example("", "", "Example: loyalty double points", "Double points every Tuesday."),
    ),
    list_url="dash-specials-hours", **_SF_COMMON,
))
_register(Dataset(
    key="hours", label="Store hours", form_cls=StoreFactForm, model=StoreFact,
    key_fields=("store", "label"), fixed={"kind": "hours"}, help=_SF_HELP,
    examples=(
        _sf_example("", "yakima", "Example: Yakima hours", "9 AM to 11 PM daily."),
        _sf_example("", "pullman", "Example: Pullman hours", "10 AM to 10 PM daily.", confirmed="no"),
    ),
    list_url="dash-specials-hours", **_SF_COMMON,
))
_register(Dataset(
    key="specials-hours", label="Specials and hours", form_cls=StoreFactForm, model=StoreFact,
    key_fields=("store", "kind", "label"), qs_filter={"kind__in": ("special", "hours")},
    choice_override={"kind": ("special", "hours")}, help=_SF_HELP,
    new_initial={"kind": "special"},
    examples=(
        _sf_example("special", "yakima", "Example: Monday flower deal", "20% off all flower on Mondays."),
        _sf_example("hours", "yakima", "Example: Yakima hours", "9 AM to 11 PM daily."),
    ),
    list_url="dash-specials-hours", **_SF_COMMON,
))
_register(Dataset(
    key="store-facts", label="Store facts (all kinds)", form_cls=StoreFactForm, model=StoreFact,
    key_fields=("store", "kind", "label"), help=_SF_HELP,
    examples=(
        _sf_example("address", "yakima", "Example: Yakima address", "1 Example St, Yakima WA"),
        _sf_example("payment", "", "Example: payment", "Cash and debit accepted."),
    ),
    list_url="dash-kb-source", **_SF_COMMON,
))
_register(Dataset(
    key="faq", label="FAQ", form_cls=FAQEntryForm, model=FAQEntry, key_fields=("key",),
    help={"key": "Short unique id (letters, numbers, hyphens). Re-uploading the same key updates the row.",
          "question": "The question as a caller would ask it.", "answer": "What the agent may say.",
          "store": _STORE_HELP, "topic": "hours, payment, pickup, returns, limits, specials, general or age.",
          "weight": "Higher = preferred when two rows match (default 100).", "is_active": _ACTIVE_HELP},
    examples=(
        {"key": "example-parking", "question": "Is there parking?", "answer": "Yes, free lot out front.",
         "store": "", "topic": "general", "source_url": "", "weight": "100", "is_active": "no"},
        {"key": "example-bags", "question": "Do you charge for bags?", "answer": "No.",
         "store": "yakima", "topic": "general", "source_url": "", "weight": "100", "is_active": "no"},
    ),
    list_fields=("key", "question", "store", "topic", "weight", "is_active"),
    headers=("Key", "Question", "Store", "Topic", "Weight", "Active"),
    list_url="dash-kb-source", paginate=True, active_field="is_active", stores_strict=True,
    kb_kind="faq", old_edit_name="dash-kb-row-edit", old_delete_name="dash-kb-row-delete",
))
_register(Dataset(
    key="education", label="Education docs", form_cls=EducationDocForm, model=EducationDoc,
    key_fields=("slug",),
    help={"slug": "Short unique id (letters, numbers, hyphens).", "is_active": _ACTIVE_HELP,
          "provisional": "yes until the final house copy is in."},
    examples=(
        {"slug": "example-edibles", "title": "Example: edibles basics", "topic": "edibles",
         "body": "Start low and go slow.", "source_url": "", "provisional": "yes", "weight": "80",
         "is_active": "no"},
        {"slug": "example-storage", "title": "Example: storage", "topic": "storage",
         "body": "Keep it cool, dark and sealed.", "source_url": "", "provisional": "yes",
         "weight": "80", "is_active": "no"},
    ),
    list_fields=("slug", "title", "topic", "weight", "is_active"),
    headers=("Slug", "Title", "Topic", "Weight", "Active"),
    list_url="dash-kb-source", paginate=True, active_field="is_active",
    kb_kind="education", old_edit_name="dash-kb-row-edit", old_delete_name="dash-kb-row-delete",
))
_register(Dataset(
    key="blog", label="Blog docs", form_cls=BlogDocForm, model=BlogDoc, key_fields=("slug",),
    help={"slug": "Short unique id (letters, numbers, hyphens).", "is_active": _ACTIVE_HELP,
          "provisional": "yes until the final house copy is in."},
    examples=(
        {"slug": "example-first-post", "title": "Example: first post", "body": "Short post body.",
         "source_url": "", "provisional": "yes", "weight": "60", "is_active": "no"},
        {"slug": "example-second-post", "title": "Example: second post", "body": "Another body.",
         "source_url": "", "provisional": "yes", "weight": "60", "is_active": "no"},
    ),
    list_fields=("slug", "title", "weight", "is_active"),
    headers=("Slug", "Title", "Weight", "Active"),
    list_url="dash-kb-source", paginate=True, active_field="is_active",
    kb_kind="blog", old_edit_name="dash-kb-row-edit", old_delete_name="dash-kb-row-delete",
))
_register(Dataset(
    key="taxonomy", label="Weights and types", form_cls=TaxonomyForm, model=WeightTypeTaxonomy,
    key_fields=("axis", "term"),
    help={"axis": "Which table the term belongs to.", "term": "The word callers use (eighth, microdose...).",
          "value": "Its canonical value (3.5 g).", "is_active": _ACTIVE_HELP},
    examples=(
        {"axis": "weight", "term": "example-eighth", "value": "3.5 g", "notes": "Example row.",
         "weight": "90", "is_active": "no"},
        {"axis": "edible_dose", "term": "example-microdose", "value": "1-2.5 mg THC",
         "notes": "Example row.", "weight": "90", "is_active": "no"},
    ),
    list_fields=("axis", "term", "value", "weight", "is_active"),
    headers=("Axis", "Term", "Value", "Weight", "Active"),
    list_url="dash-kb-source", paginate=True, active_field="is_active",
    kb_kind="taxonomy", old_edit_name="dash-kb-row-edit", old_delete_name="dash-kb-row-delete",
))
_register(Dataset(
    key="policy-categories", label="Policy categories", form_cls=PolicyCategoryForm,
    model=PolicyCategory, key_fields=("slug",),
    help={"slug": "Short unique id. Re-uploading the same slug updates the category.",
          "topic": "Blank = findable by any question; or return_policy, specials, hours_location.",
          "is_active": _ACTIVE_HELP},
    examples=(
        {"slug": "example-delivery", "label": "Example: delivery", "description": "Delivery rules.",
         "topic": "", "weight": "120", "is_active": "no", "order": "0"},
        {"slug": "example-id-rules", "label": "Example: ID rules", "description": "What ID we accept.",
         "topic": "", "weight": "120", "is_active": "no", "order": "1"},
    ),
    list_fields=("slug", "label", "topic", "weight", "is_active"),
    headers=("Slug", "Label", "Topic", "Weight", "Active"),
    list_url="dash-policies", active_field="is_active", can_delete=_protected_category,
    old_edit_name="dash-policies-category-edit", old_delete_name="dash-policies-category-delete",
))
_register(Dataset(
    key="vendor-allowlist", label="Vendor allowlist", form_cls=VendorAllowlistEntryForm,
    model=VendorAllowlistEntry, key_fields=("phone",), phone_key=True,
    help={"phone": "A full US number. (509) 555-1212 and +15095551212 both work; this is the key.",
          "store": "A label only: which store the vendor serves (blank, yakima, mount-vernon or pullman).",
          "active": "yes = this number rings the owner; no = kept but ignored."},
    examples=(
        {"name": "Example Distribution", "phone": "(509) 555-0100", "store": "", "note": "rep: Sam",
         "active": "no"},
        {"name": "Example Farms", "phone": "(360) 555-0101", "store": "yakima", "note": "",
         "active": "no"},
    ),
    list_fields=("name", "phone", "store", "note", "active"),
    headers=("Name", "Number", "Store", "Note", "State", "Last match", "Matches"),
    list_url="dash-vendor-allowlist", active_field="active",
    row_template="dashboard/_data_row_allowlist.html",
    old_edit_name="dash-vendor-allowlist-edit", old_delete_name="dash-vendor-allowlist-delete",
    ordering=("name", "id"),
))
# Policy documents have no natural key (a title is not unique), so no CSV; the dataset exists so the
# KB "policy" list gets inline edit, Edit all and row actions like every other KB list.
_register(Dataset(
    key="policy-docs", label="Policy documents", form_cls=PolicyForm, model=PolicyDocument, csv=False,
    list_fields=("title", "category", "weight", "is_active"),
    headers=("Title", "Category", "Weight", "Active"),
    list_url="dash-kb-source", paginate=True, active_field="is_active", ordering=("id",),
    kb_kind="policy", old_edit_name="dash-kb-row-edit", old_delete_name="dash-kb-row-delete",
))

# KB source slug -> dataset key (the generic KB list pages)
KB_DATASET = {
    "faq": "faq", "policy": "policy-docs", "store-fact": "store-facts",
    "education": "education", "blog": "blog", "taxonomy": "taxonomy",
}
assert set(KB_DATASET) == set(KB_FORMS)  # a new KB kind must be wired here too


def get_dataset(key: str) -> Dataset | None:
    return DATASETS.get(key)


def csv_datasets() -> list[Dataset]:
    return [d for d in DATASETS.values() if d.csv]


# ── decoding + parsing ─────────────────────────────────────────────────────────────────────────
def decode_bytes(raw: bytes) -> str:
    """UTF-8, UTF-8 BOM, UTF-16 (BOM or NUL pattern) or Windows-1252, in that order."""
    if not raw.strip():
        raise BulkError("The file is empty.")
    if raw[:2] == b"PK":
        raise BulkError(
            "This is an Excel workbook, not a CSV. In Excel use File > Save As > CSV UTF-8, then upload that."
        )
    try:
        if raw.startswith(codecs.BOM_UTF8):
            text = raw[3:].decode("utf-8")
        elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            text = raw.decode("utf-16")
        elif raw[1:2] == b"\x00" and raw[:1] != b"\x00":
            text = raw.decode("utf-16-le")
        elif raw[:1] == b"\x00" and raw[1:2] != b"\x00":
            text = raw.decode("utf-16-be")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise BulkError("Could not read the file's text encoding. Save it as CSV UTF-8 and try again.") from exc
    if "\x00" in text:
        raise BulkError("This does not look like a text CSV file.")
    return text.lstrip("﻿")


def _norm_header(h: str) -> str:
    return re.sub(r"[\s\-]+", "_", h.strip().lower())


def _sniff_delimiter(text: str) -> str:
    first = text.split("\n", 1)[0]
    counts = {d: first.count(d) for d in (",", "\t", ";")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


@dataclass
class RawRow:
    line: int  # spreadsheet row number (header = 1)
    cells: dict
    problem: str = ""


@dataclass
class ParsedFile:
    text: str
    columns: list[str] = field(default_factory=list)  # recognised, normalised, in file order
    unknown: list[tuple[str, str]] = field(default_factory=list)  # (header as written, suggestion)
    rows: list[RawRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # file-level problems: nothing can be applied


def parse_text(ds: Dataset, text: str) -> ParsedFile:
    parsed = ParsedFile(text=text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_sniff_delimiter(text))
    records = iter(reader)
    try:
        header = next(records)
    except StopIteration:
        parsed.errors.append("The file is empty.")
        return parsed
    except csv.Error as exc:
        parsed.errors.append(f"Row 1 could not be read as CSV ({exc}).")
        return parsed
    known = set(ds.file_columns())
    names: list[str | None] = []
    seen: set[str] = set()
    for h in header:
        n = _norm_header(h)
        if n in known:
            if n in seen:
                parsed.errors.append(f"The column '{n}' appears more than once in the header.")
            seen.add(n)
            names.append(n)
        else:
            names.append(None)
            if n:
                close = difflib.get_close_matches(n, sorted(known), n=1, cutoff=0.6)
                parsed.unknown.append((h.strip(), close[0] if close else ""))
    parsed.columns = [n for n in names if n]
    missing = [k for k in ds.key_fields if k not in seen]
    if missing:
        parsed.errors.append(
            "Missing column(s) the file needs to find each row: " + ", ".join(missing) + "."
        )
    if parsed.errors:
        return parsed
    n_rows = 0
    row_no = 1
    while True:
        row_no += 1
        try:
            rec = next(records)
        except StopIteration:
            break
        except csv.Error as exc:
            parsed.errors.append(f"Row {row_no} could not be read as CSV ({exc}).")
            return parsed
        if not any(c.strip() for c in rec):
            continue
        n_rows += 1
        if n_rows > MAX_ROWS:
            parsed.errors.append(f"Too many rows: the limit is {MAX_ROWS:,} rows per file.")
            parsed.rows = []
            return parsed
        cells: dict[str, str] = {}
        problem = ""
        for i, value in enumerate(rec):
            if i < len(names):
                if names[i]:
                    cells[names[i]] = value
            elif value.strip():
                problem = "This row has more cells than the header has columns."
        parsed.rows.append(RawRow(row_no, cells, problem))
    if not parsed.rows:
        parsed.errors.append("The file has a header but no data rows.")
    return parsed


def parse_upload(ds: Dataset, raw: bytes) -> ParsedFile:
    if len(raw) > MAX_BYTES:
        raise BulkError("The file is larger than 1 MB.")
    return parse_text(ds, decode_bytes(raw))


# ── cell cleaning ──────────────────────────────────────────────────────────────────────────────
def clean_cell(raw: str) -> tuple[str, str]:
    """Import-side formula guard -> (value, error). See the module docstring for the rule."""
    v = raw.strip()
    if v[:1] == "'" and v[1:2] in ("=", "+", "-", "@"):
        return v[1:].strip(), ""  # the apostrophe the exporter (or the author) put in front
    if v[:1] in ("=", "@"):
        return v, ("starts with '" + v[0] + "' (a spreadsheet formula character). Remove it, or put an "
                   "apostrophe in front if it really is text")
    if v[:1] in ("+", "-") and not _NUMBERISH.fullmatch(v):
        return v, ("starts with '" + v[0] + "' (a spreadsheet formula character). Remove it, or put an "
                   "apostrophe in front if it really is text")
    return v, ""


def parse_bool(v: str) -> bool | None:
    s = v.strip().lower()
    if not s:
        return None
    if s in _YES:
        return True
    if s in _NO:
        return False
    raise ValueError(v)


def _normalise(ds: Dataset, name: str, v: str) -> tuple[str, str]:
    if name == "store" and ds.stores_strict:
        v = v.lower()
        if v not in ("", *STORES):
            return v, "must be blank or one of " + ", ".join(STORES)
    elif name == "phone" and ds.phone_key:
        from voice.vendor_allowlist import normalize_us_e164

        e164 = normalize_us_e164(v)
        if not e164:
            return v, "is not a full US phone number"
        return e164, ""
    elif name in ds.choice_override and v and v not in ds.choice_override[name]:
        return v, "must be one of " + ", ".join(ds.choice_override[name])
    return v, ""


# ── planning ───────────────────────────────────────────────────────────────────────────────────
@dataclass
class RowPlan:
    line: int
    action: str  # create | update | unchanged | delete | error
    key: tuple = ()
    errors: list[str] = field(default_factory=list)
    changes: list[tuple[str, str, str]] = field(default_factory=list)  # (field, old, new)
    form: object = None
    obj: object = None
    note: str = ""

    @property
    def key_display(self) -> str:
        return " / ".join(str(k) if str(k) != "" else "(blank)" for k in self.key)


@dataclass
class Plan:
    rows: list[RowPlan] = field(default_factory=list)
    file_errors: list[str] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)
    allow_deletes: bool = False

    @property
    def counts(self) -> dict:
        c = {"create": 0, "update": 0, "unchanged": 0, "delete": 0, "error": 0}
        for r in self.rows:
            c[r.action] += 1
        return c

    @property
    def errors(self) -> list[RowPlan]:
        return [r for r in self.rows if r.action == "error"]

    @property
    def digest(self) -> str:
        data = [(r.line, r.action, list(r.key), sorted(c[0] for c in r.changes)) for r in self.rows]
        return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()[:32]

    @property
    def preview_rows(self) -> list[RowPlan]:
        return [r for r in self.rows if r.action in ("create", "update", "delete")][:20]


def _initial_data(ds: Dataset, instance) -> tuple[dict, forms.BaseForm]:
    form = ds.form_cls(instance=instance) if instance is not None else ds.form_cls(initial=ds.new_initial)
    data: dict[str, str] = {}
    for name, bf in form.fields.items():
        v = form.initial.get(name, bf.initial)
        if isinstance(bf, forms.BooleanField):
            if v:
                data[name] = "on"
        elif v is None:
            data[name] = ""
        elif isinstance(v, datetime.date):
            data[name] = v.isoformat()
        elif hasattr(v, "pk"):
            data[name] = str(v.pk)
        else:
            data[name] = str(v)
    return data, form


def build_form(ds: Dataset, instance, overrides: dict[str, str]):
    """A bound ModelForm: the instance's current values with ``overrides`` (cell strings) on top.
    Returns (form, errors). A bool/int override that is blank keeps the current value."""
    data, base = _initial_data(ds, instance)
    errors: list[str] = []
    for name, v in overrides.items():
        f = base.fields.get(name)
        if f is None:
            continue
        if isinstance(f, forms.BooleanField):
            try:
                b = parse_bool(v)
            except ValueError:
                errors.append(f"{f.label or name}: '{v}' is not yes or no")
                continue
            if b is None:
                continue
            if b:
                data[name] = "on"
            else:
                data.pop(name, None)
        elif isinstance(f, forms.IntegerField) and not v:
            continue
        else:
            data[name] = v
    data.update({k: str(v) for k, v in ds.fixed.items()})
    form = ds.form_cls(data, instance=instance) if instance is not None else ds.form_cls(data)
    return form, errors


def flat_errors(form) -> list[str]:
    out = []
    for name, errs in form.errors.items():
        label = "" if name == "__all__" else str(form.fields[name].label or name) + ": "
        out.extend(label + e for e in errs)
    return out


def judge(ds: Dataset, line: int, obj, form, key: tuple = ()) -> RowPlan:
    """Validate a bound form against ``obj`` (None = create) -> a RowPlan."""
    if obj is not None and not form.has_changed():
        return RowPlan(line, "unchanged", key, form=form, obj=obj)
    if not form.is_valid():
        return RowPlan(line, "error", key, errors=flat_errors(form), obj=obj)
    changes = []
    if obj is None:
        for name in ds.form_fields():
            new = fmt_value(form.cleaned_data.get(name))
            if new:
                changes.append((name, "", new))
        return RowPlan(line, "create", key, changes=changes, form=form)
    for name in form.changed_data:
        changes.append((name, fmt_value(form.initial.get(name)), fmt_value(form.cleaned_data.get(name))))
    return RowPlan(line, "update", key, changes=changes, form=form, obj=obj)


def _key_of(ds: Dataset, obj) -> tuple:
    return tuple(str(getattr(obj, k, "") or "").strip() for k in ds.key_fields)


def plan_file(ds: Dataset, parsed: ParsedFile, *, allow_deletes: bool = False) -> Plan:
    plan = Plan(file_errors=list(parsed.errors), unknown=list(parsed.unknown), allow_deletes=allow_deletes)
    if parsed.errors:
        return plan
    labels = {c.name: c.label for c in ds.columns()}
    existing = {_key_of(ds, o): o for o in ds.queryset()}
    # pass 1: clean every cell, derive keys
    prepared = []
    first_seen: dict[tuple, list[int]] = {}
    for raw in parsed.rows:
        errs = [raw.problem] if raw.problem else []
        cells: dict[str, str] = {}
        bad: set[str] = set()
        for name, value in raw.cells.items():
            v, err = clean_cell(value)
            if not err and name != DELETE_COL:
                v, err = _normalise(ds, name, v)
            if err:
                errs.append(f"{labels.get(name, name)}: {err}")
                bad.add(name)
                continue
            cells[name] = v
        key = tuple(cells.get(k, "") for k in ds.key_fields)
        for k, kv in zip(ds.key_fields, key, strict=True):
            if not kv and k not in bad and ds.form_cls.base_fields[k].required:
                errs.append(f"{labels.get(k, k)}: this is the row's key and cannot be blank")
        prepared.append((raw, cells, key, errs))
        if not errs:
            first_seen.setdefault(key, []).append(raw.line)
    # pass 2: plan each row
    for raw, cells, key, errs in prepared:
        if not errs and len(first_seen.get(key, ())) > 1:
            others = [str(n) for n in first_seen[key] if n != raw.line]
            errs.append("Duplicate key: the same row also appears on row " + ", ".join(others))
        if errs:
            plan.rows.append(RowPlan(raw.line, "error", key, errors=errs))
            continue
        obj = existing.get(key)
        try:
            wants_delete = bool(parse_bool(cells.get(DELETE_COL, "")))
        except ValueError:
            plan.rows.append(RowPlan(raw.line, "error", key, errors=[
                f"delete: '{cells.get(DELETE_COL)}' is not yes or no"]))
            continue
        if wants_delete:
            plan.rows.append(_plan_delete(ds, raw.line, key, obj, allow_deletes))
            continue
        overrides = {k: v for k, v in cells.items() if k != DELETE_COL}
        form, ferrs = build_form(ds, obj, overrides)
        if ferrs:
            plan.rows.append(RowPlan(raw.line, "error", key, errors=ferrs))
            continue
        plan.rows.append(judge(ds, raw.line, obj, form, key))
    return plan


def _plan_delete(ds: Dataset, line: int, key: tuple, obj, allow_deletes: bool) -> RowPlan:
    if not allow_deletes:
        return RowPlan(line, "error", key, errors=[
            "delete is yes, but 'Allow deletes' was not ticked, so this row is not deleted"])
    if obj is None:
        return RowPlan(line, "unchanged", key, note="nothing to delete: no such row")
    blocked = ds.can_delete(obj) if ds.can_delete else ""
    if blocked:
        return RowPlan(line, "error", key, errors=[f"cannot delete: {blocked}"])
    return RowPlan(line, "delete", key, obj=obj, changes=[("delete", "", "yes")])


def plan_edits(ds: Dataset, items: list[tuple[object, dict[str, str]]]) -> Plan:
    """Plan an in-app edit of existing rows: ``[(obj, {field: new string})]`` (row action 'set one
    field', activate/deactivate). Line numbers are 1-based positions in the selection."""
    plan = Plan()
    for i, (obj, overrides) in enumerate(items, start=1):
        form, ferrs = build_form(ds, obj, overrides)
        if ferrs:
            plan.rows.append(RowPlan(i, "error", (str(obj),), errors=ferrs, obj=obj))
        else:
            plan.rows.append(judge(ds, i, obj, form, (str(obj),)))
    return plan


# ── applying ───────────────────────────────────────────────────────────────────────────────────
def run_side_effects(ds: Dataset, summary: dict) -> None:
    """THE once-per-batch hook (tests mock this and assert it ran once). Called after a batch has
    committed, never per row. Today that is the budtender "store-facts" refresh for StoreFact
    datasets -- the same effect a single StoreFact save sends from kb/signals.py."""
    if not (summary["created"] or summary["updated"] or summary["deleted"]):
        return
    if ds.notify_store_facts:
        from voice import tasks

        tasks.dispatch_budtender_notify("store-facts")


def apply_plan(ds: Dataset, plan: Plan, *, user=None, action: str = "upload",
               stop_on_error: bool = False) -> dict:
    """Write every create/update/delete row of ``plan`` in ONE transaction.

    Transaction semantics: all planned rows commit together or none do (a database error part-way
    rolls the whole batch back and nothing is reported as saved). Rows that failed VALIDATION were
    already left out of the plan's writes; with ``stop_on_error`` any such row aborts the whole
    batch before the first write. Per-row signals are held for the batch and ``run_side_effects``
    runs once after commit."""
    from kb import signals

    counts = plan.counts
    summary = {"created": 0, "updated": 0, "unchanged": counts["unchanged"], "deleted": 0,
               "errors": counts["error"], "applied": False, "stopped": False, "failed": ""}
    if stop_on_error and counts["error"]:
        summary["stopped"] = True
        return summary
    try:
        with transaction.atomic(), signals.bulk(publish=False, nudges=()):
            for r in plan.rows:
                if r.action in ("create", "update"):
                    r.form.save()
                    summary["created" if r.action == "create" else "updated"] += 1
                elif r.action == "delete":
                    try:
                        with transaction.atomic():
                            r.obj.delete()
                    except ProtectedError:
                        r.action, r.errors = "error", ["cannot delete: other rows still depend on it"]
                        summary["errors"] += 1
                        continue
                    summary["deleted"] += 1
    except DatabaseError:
        logger.exception("bulk %s on %s rolled back", action, ds.key)
        summary.update(created=0, updated=0, deleted=0, failed="The database refused the batch; nothing was saved.")
        return summary
    summary["applied"] = True
    run_side_effects(ds, summary)
    log_batch(ds, user, action, summary, total=len(plan.rows))
    return summary


def log_batch(ds: Dataset, user, action: str, summary: dict, *, total: int) -> None:
    """One audit row + one log line: dataset, counts, who. Never the data itself."""
    from dashboard.models import BulkBatchLog

    username = getattr(user, "get_username", lambda: "")() if user is not None else ""
    logger.info(
        "bulk %s dataset=%s user=%s rows=%d created=%d updated=%d deleted=%d unchanged=%d errors=%d",
        action, ds.key, username, total, summary["created"], summary["updated"], summary["deleted"],
        summary["unchanged"], summary["errors"],
    )
    BulkBatchLog.objects.create(
        dataset=ds.key, action=action, username=username[:150], rows=total,
        created=summary["created"], updated=summary["updated"], deleted=summary["deleted"],
        unchanged=summary["unchanged"], errors=summary["errors"],
    )
