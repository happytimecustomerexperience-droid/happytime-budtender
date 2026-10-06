"""Batch labs and product details: the durable tables (BatchLab, ProductDetail), the paced warm
job that fills them, and the DB-only read side the chat's picks use.

Write side (a paced job, NEVER a request): `warm` finds in-stock products whose batch has no lab (or
whose product has no fresh detail) and fetches each through ONE pinned Backoffice client:
`new_drops.lab_for_batch` (one lab reader, one normalizer, `new_drops.summarize_lab`) and
`BackofficeClient.get_product_details` (allowlisted by `product_detail.info_from_data`). Dutchie
answering "no data" is stored as 'none'; Dutchie not answering, or answering with an empty or
unstructured body, stores NOTHING: empty is not unknown, and an anomaly is not an answer.

Read side (request path, DB only, one bulk query per table): `labs_for` / `details_for` fill a
per-request `Memo`; `enqueue_missing` hands whatever is missing or stale to ONE deduped task so it
fills for the next viewer. The request path never calls Dutchie.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from dutchie.session import DutchieRejected

from . import new_drops, product_detail, terpenes
from .models import BatchLab, Product, ProductDetail

logger = logging.getLogger(__name__)

RECHECK_NONE_AFTER = timedelta(days=7)   # a batch with no lab on file is asked again after this
RECHECK_OK_AFTER = timedelta(days=30)    # a stored lab is re-fetched after this, so a correction can land
FAIL_LIMIT = 3             # an id that failed this many times (or once, per-id) is skipped for FAIL_SKIP
FAIL_SKIP = 24 * 3600
DETAIL_REFRESH_AFTER = timedelta(days=7)  # product details are mutable: refreshed after this
MAX_PER_RUN = 100          # lookups per store per run (per half)
STOP_AFTER_FAILURES = 3    # identical consecutive failures end a run
DEFAULT_PAUSE = 0.5        # extra seconds between network calls, on top of the client's own pacing
ENQUEUE_DEDUPE = 30 * 60   # an id handed to the warm task is not handed over again for this long
ENQUEUE_BREAKER = 60       # after a broker failure the request path stops trying for this long
WARM_WHAT = ("labs", "details", "both")


# ── pure ─────────────────────────────────────────────────────────────────────
def lab_from_data(data: dict, category: str) -> dict:
    """Dutchie lab-results Data -> the Contract-A `lab` dict WITHOUT `profile`."""
    s = new_drops.summarize_lab(data, category or "", top=5, detail=True)
    return {
        "total_terpenes": s["total_terpenes"],
        "terpenes": [{"name": t["name"], "pct": t["value"]} for t in s["terpenes"] if t["value"] > 0],
        "cbd_total": s["cbd"],
        "thc_total": s["thc"],
        "minor_cannabinoids": s["minor_cannabinoids"],
        "tested_date": s["tested_date"],
        "lab_name": s["lab_name"],
        "coa_url": s["coa_url"],
        "contaminants": s["contaminants"],
    }


def is_empty(lab: dict) -> bool:
    """Nothing usable on file: no terpenes, no totals, no minors, no passed contaminants, no COA link."""
    return not (lab["terpenes"] or lab["total_terpenes"] or lab["thc_total"] or lab["cbd_total"]
                or lab["minor_cannabinoids"] or lab["contaminants"] or lab["coa_url"])


def with_profile(lab: dict) -> dict:
    """The stored lab plus `profile`, built from its numbers (the stored dict is not touched)."""
    return {**lab, "profile": terpenes.profile(lab.get("terpenes"))}


_DATE = re.compile(r"\d{4}-\d\d-\d\d")


def _positive(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _entries(rows, limit: int) -> list[dict]:
    """[{name, pct}] from stored rows: only a string name and a positive number survive, nothing else."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"].strip() \
                and _positive(row.get("pct")):
            out.append({"name": row["name"].strip()[:60], "pct": float(row["pct"])})
    return out[:limit]


def public_lab(lab) -> dict | None:
    """What a customer may see of a stored lab: ONLY the Contract-A keys, each re-validated, plus the
    `profile` the numbers produce. A stored row is never trusted to be clean, so an extra key (a stray
    cost, a note, a field added later) cannot reach a customer, and a malformed value becomes null.
    `thc_total` is deliberately not here: thc_percent is the ONE potency number (the lab's value only
    fills it when inventory has none, see effective_thc)."""
    if not lab or not isinstance(lab, dict):
        return None
    terps = _entries(lab.get("terpenes"), 5)
    date = lab.get("tested_date")
    name = lab.get("lab_name")
    contaminants = lab.get("contaminants") if isinstance(lab.get("contaminants"), dict) else {}
    return {
        "total_terpenes": _positive(lab.get("total_terpenes")),
        "terpenes": terps,
        "cbd_total": _positive(lab.get("cbd_total")),
        "minor_cannabinoids": _entries(lab.get("minor_cannabinoids"), 3),
        "tested_date": date if isinstance(date, str) and _DATE.fullmatch(date) else None,
        "lab_name": name.strip()[:120] or None if isinstance(name, str) else None,
        "coa_url": new_drops.https_url(lab.get("coa_url")) or None,
        "contaminants": {k: "pass" for k in ("pesticides", "heavy_metals", "mycotoxin", "microbiology", "solvents")
                         if contaminants.get(k) == "pass"},
        "profile": terpenes.profile(terps),
    }


def effective_thc(thc_percent, lab: dict | None):
    """THE displayed potency: the inventory value, else the lab's total THC, else None."""
    if thc_percent is not None:
        return thc_percent
    return (lab or {}).get("thc_total")


def dominant_terpene(lab: dict | None) -> str:
    """Canonical name of the strongest terpene ('myrcene'), or ''."""
    top = ((lab or {}).get("terpenes") or [])[:1]
    return terpenes.canonical(top[0].get("name")) if top else ""


# ── read side (request path: DB only, one bulk query per table) ──────────────
class Memo(dict):
    """One request's memo of stored rows: {id: stored data | {} | None}. A hit holds the data; `{}`
    means Dutchie was asked recently and there is nothing to show; None means nothing is stored.
    `.stale` collects the ids that need a (re)warm: never fetched, or due again."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale: set[str] = set()


def _bulk(model, key: str, ids, memo, refresh_after: timedelta | None):
    """Fill `memo` for the ids it has not answered, with ONE query. A failed read answers
    "nothing stored" for this call only: it is neither memoized nor marked stale."""
    stale = getattr(memo, "stale", None)  # a plain dict memo simply cannot record staleness
    wanted = {i for i in ids if i and i not in memo}
    if not wanted:
        return
    try:
        rows = {r[0]: r[1:] for r in model.objects.filter(**{f"{key}__in": wanted})
                .values_list(key, "status", "data", "checked_at")}
    except Exception:  # noqa: BLE001 - a lab/detail read must never take a search down
        logger.warning("%s read failed for %d ids", model.__name__, len(wanted), exc_info=True)
        return
    cutoff = datetime.now(timezone.utc) - (refresh_after or RECHECK_NONE_AFTER)
    for i in wanted:
        status, data, checked_at = rows.get(i, (None, None, None))
        fresh = checked_at is not None and checked_at >= cutoff
        if status == "ok":
            memo[i] = data
            # a batch lab is immutable (always fresh); a product detail is served stale while it refreshes
            if refresh_after is not None and not fresh and stale is not None:
                stale.add(i)
        elif status == "none" and fresh:
            memo[i] = {}
        else:
            memo[i] = None
            if stale is not None:
                stale.add(i)


def labs_for(batch_ids, memo: dict | None = None) -> dict[str, dict]:
    """{batch_id: stored lab} for the ids that have an 'ok' lab. ONE query for the ids the memo has
    not already answered; hits AND misses are memoized, so a request asks at most once per id."""
    memo = Memo() if memo is None else memo
    _bulk(BatchLab, "batch_id", batch_ids, memo, None)
    return {b: memo[b] for b in batch_ids if b and memo.get(b)}


def details_for(product_ids, memo: dict | None = None) -> dict[str, dict]:
    """{product_id: stored info} for the ids that have an 'ok' ProductDetail (stale info is still
    served while it refreshes). ONE query for the ids the memo has not already answered."""
    memo = Memo() if memo is None else memo
    _bulk(ProductDetail, "product_id", product_ids, memo, DETAIL_REFRESH_AFTER)
    return {p: memo[p] for p in product_ids if p and memo.get(p)}


def enqueue_missing(location_slug: str, products, labs, details) -> int:
    """Hand the shown picks' missing/stale batch labs and product details to ONE warm task, so they
    fill for the next viewer. Deduped per id (~30 min); a down broker or cache never fails the
    request: it is logged, and the request path stops trying for a minute. Never calls Dutchie."""
    try:
        batches = [b for b in dict.fromkeys(p.batch_id for p in products) if b and b in getattr(labs, "stale", ())]
        pids = [x for x in dict.fromkeys(p.product_id for p in products) if x and x in getattr(details, "stale", ())]
        if not (batches or pids) or _eager() or cache.get("labwarm:down"):
            return 0
        # Peek, send, and only THEN remember: a broker failure must not suppress the retry for 30 minutes.
        batches = [b for b in batches if not cache.get(f"labwarm:b:{b}")]
        pids = [x for x in pids if not cache.get(f"labwarm:p:{x}")]
        if not (batches or pids):
            return 0
        from . import tasks  # lazy: tasks imports this module

        try:
            tasks.warm_ids.apply_async(args=[location_slug, batches, pids], retry=False)
        except Exception:  # noqa: BLE001 - a down broker must not fail or slow a search
            cache.set("labwarm:down", 1, ENQUEUE_BREAKER)
            logger.warning("could not enqueue the lab/detail warm (broker down?); the beat will cover it",
                           exc_info=True)
            return 0
        for b in batches:
            cache.add(f"labwarm:b:{b}", 1, ENQUEUE_DEDUPE)
        for x in pids:
            cache.add(f"labwarm:p:{x}", 1, ENQUEUE_DEDUPE)
        return len(batches) + len(pids)
    except Exception:  # noqa: BLE001
        logger.warning("lab/detail warm enqueue skipped", exc_info=True)
        return 0


def _eager() -> bool:
    """Celery running tasks inline (CELERY_EAGER / task_always_eager)? Then a "queued" warm would make
    Dutchie calls on the request thread, so nothing is queued."""
    from celery import current_app

    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or current_app.conf.task_always_eager)


def for_picks(location_slug: str, products, labs: dict | None = None) -> tuple[Memo, Memo]:
    """(labs, details) memos for the picks about to be serialized: one bulk read per table (a memo the
    ranker already filled costs nothing), then whatever is missing is enqueued for the next viewer."""
    labs = Memo() if labs is None else labs
    details = Memo()
    labs_for([p.batch_id for p in products], memo=labs)
    details_for([p.product_id for p in products], memo=details)
    enqueue_missing(location_slug, products, labs, details)
    return labs, details


# ── write side ───────────────────────────────────────────────────────────────
def record(batch_id, data, category: str, now: datetime | None = None) -> str | None:
    """Store what Dutchie said about a batch: 'ok' (a lab), or 'none' (HasLabData:false / the expected
    structure with nothing in it). An empty, missing or unstructured answer is an anomaly, not an
    answer: nothing is stored and None is returned, exactly as for no answer at all."""
    if not isinstance(data, dict):
        return None
    lab = lab_from_data(data, category)
    if not is_empty(lab):
        status = "ok"
    elif data.get("HasLabData") is False or "Terpenes" in data or "Cannabinoids" in data:
        status = "none"
        # A re-check of a batch that already has a lab must never DOWNGRADE it on one "no lab" answer
        # (a transient oddity would erase good data). Keep the lab, note that it was checked.
        kept = BatchLab.objects.filter(batch_id=str(batch_id), status="ok").update(
            checked_at=now or datetime.now(timezone.utc))
        if kept:
            logger.warning("BatchLab %s: re-check said 'no lab data'; the stored lab is kept", batch_id)
            return "ok"
    else:
        return None
    BatchLab.objects.update_or_create(
        batch_id=str(batch_id),
        defaults={"status": status, "data": {} if status == "none" else lab,
                  "checked_at": now or datetime.now(timezone.utc)})
    return status


def record_detail(product_id, data, now: datetime | None = None) -> str | None:
    """Store the allowlisted `info` for a product: 'ok', or 'none' when the record is real but has
    nothing customer-facing. No record / unstructured -> nothing stored, None."""
    if not product_detail.has_structure(data):
        return None
    info = product_detail.info_from_data(data)
    status = "ok" if info else "none"
    ProductDetail.objects.update_or_create(
        product_id=str(product_id),
        defaults={"status": status, "data": info or {}, "checked_at": now or datetime.now(timezone.utc)})
    return status


def _digits(value: str) -> bool:
    return value.isascii() and value.isdigit()  # these land in a URL path / a request body


def select_todo(location_slug: str, limit: int = MAX_PER_RUN, now: datetime | None = None,
                only_batches=None) -> list[tuple[str, str]]:
    """[(batch_id, category)] to look up: in-stock products' batches with no lab, an 'ok' lab checked
    more than 30 days ago (a stored lab can be corrected at the source), or a 'none' checked more than 7
    days ago; fastest sellers first (what the chat suggests most). An id that failed recently is skipped
    for 24 hours (see _skipped) so a few that can never be answered cannot hold the queue."""
    now = now or datetime.now(timezone.utc)
    settled = BatchLab.objects.filter(
        Q(status="ok", checked_at__gte=now - RECHECK_OK_AFTER)
        | Q(status="none", checked_at__gte=now - RECHECK_NONE_AFTER)).values("batch_id")
    qs = Product.objects.filter(location_slug=location_slug, availability=True).exclude(batch_id="")
    if only_batches is not None:
        qs = qs.filter(batch_id__in=only_batches)
    rows = qs.exclude(batch_id__in=settled).order_by("-velocity", "id").values_list("batch_id", "category")
    todo: dict[str, str] = {}
    for bid, category in rows:
        if _digits(bid) and bid not in todo and not _skipped("b", bid):
            todo[bid] = category
            if len(todo) >= limit:
                break
    return list(todo.items())


def select_detail_todo(location_slug: str, limit: int = MAX_PER_RUN, now: datetime | None = None,
                       only_products=None) -> list[str]:
    """[product_id] to look up: in-stock products with no ProductDetail checked inside the last 7 days
    (a detail is mutable, so ok and none both age out), fastest sellers first."""
    now = now or datetime.now(timezone.utc)
    fresh = ProductDetail.objects.filter(checked_at__gte=now - DETAIL_REFRESH_AFTER).values("product_id")
    qs = Product.objects.filter(location_slug=location_slug, availability=True).exclude(product_id="")
    if only_products is not None:
        qs = qs.filter(product_id__in=only_products)
    rows = qs.exclude(product_id__in=fresh).order_by("-velocity", "id").values_list("product_id", flat=True)
    todo: list[str] = []
    for pid in dict.fromkeys(rows):
        if _digits(pid) and not _skipped("p", pid):
            todo.append(pid)
            if len(todo) >= limit:
                break
    return todo


# ── ids that keep failing ────────────────────────────────────────────────────
# A permanently unanswerable id (a deleted batch, a product Dutchie 500s on) at the head of the queue would
# be retried first on every run and, once three of them led, stop every run before anything else got done.
# So a failed id is remembered: an id Dutchie answered with "nothing" / "no" (a per-id failure) is skipped for
# 24 hours at once; a transport/throttle/auth failure only counts, and FAIL_LIMIT of them in a row skip it too.
def _fail_key(kind: str, ident: str) -> str:
    return f"labwarm:fail:{kind}:{ident}"


def _skipped(kind: str, ident: str) -> bool:
    return (cache.get(_fail_key(kind, ident)) or 0) >= FAIL_LIMIT


def _mark_failed(kind: str, ident: str) -> None:
    cache.set(_fail_key(kind, ident), FAIL_LIMIT, FAIL_SKIP)


def _count_failure(kind: str, ident: str) -> None:
    cache.set(_fail_key(kind, ident), (cache.get(_fail_key(kind, ident)) or 0) + 1, FAIL_SKIP)


def _drain(items, bucket: dict, step, state: dict, kind: str) -> None:
    """Run `step(item) -> 'ok'|'none'|None` over `items`, counting into `bucket`.

    Two kinds of failure, treated differently:
      per-id    Dutchie answered but not usefully: an empty/odd answer (step returns None) or a
                Result=false rejection. That id is skipped for 24 hours; it says nothing about Dutchie's
                health, so it is NOT a stop reason and does not touch the streak.
      systemic  transport, throttle (429), auth, or anything unexpected. Three IDENTICAL consecutive ones
                set state['stopped'] and end the loop (hammering a down Dutchie helps nobody); each also
                counts against that id, which is skipped once it has failed FAIL_LIMIT times."""
    done: set[str] = set()
    for item in items:
        ident = item[0] if isinstance(item, tuple) else item
        try:
            status = step(item)
            failure, systemic = (None, False) if status else ("no-answer", False)
        except DutchieRejected as exc:
            logger.warning("warm_batch_labs %s: rejected: %s", ident, exc)
            status, failure, systemic = None, "rejected", False
        except Exception as exc:  # noqa: BLE001 - one item's transport/parse/DB error is a failure, not a crash
            logger.warning("warm_batch_labs %s: %s: %s", ident, type(exc).__name__, exc)
            status, failure, systemic = None, type(exc).__name__, True
        if failure is None:
            bucket[status] += 1
            done.add(ident)
            state["last"], state["streak"] = None, 0
            cache.delete(_fail_key(kind, ident))
            continue
        bucket["failed"] += 1
        if not systemic:
            _mark_failed(kind, ident)
            continue
        _count_failure(kind, ident)
        state["streak"] = state["streak"] + 1 if failure == state["last"] else 1
        state["last"] = failure
        if state["streak"] >= STOP_AFTER_FAILURES:
            state["stopped"] = True
            break
    bucket["unresolved"] = [(i[0] if isinstance(i, tuple) else i) for i in items
                            if (i[0] if isinstance(i, tuple) else i) not in done]


def warm(location_slug: str, limit: int = MAX_PER_RUN, pause: float | None = None, dry_run: bool = False,
         now: datetime | None = None, what: str = "both", only_batches=None, only_products=None) -> dict:
    """Fill BatchLab and/or ProductDetail for one store, paced and sequential, through ONE pinned
    client (one login). Stops after 3 identical consecutive failures (across both halves) and logs the
    unresolved ids. `only_*` restricts a run to given ids (the on-demand task). Returns a summary dict."""
    if what not in WARM_WHAT:
        raise ValueError(f"what must be one of {WARM_WHAT}")
    now = now or datetime.now(timezone.utc)
    labs_todo = select_todo(location_slug, limit, now, only_batches) if what != "details" else []
    details_todo = select_detail_todo(location_slug, limit, now, only_products) if what != "labs" else []
    if dry_run:
        out = {"store": location_slug, "would_fetch": [b for b, _ in labs_todo]}
        if what != "labs":
            out["would_fetch_details"] = details_todo
        return out
    out = {"store": location_slug, "selected": len(labs_todo), "ok": 0, "none": 0, "failed": 0,
           "stopped": False, "unresolved": []}
    if what != "labs":
        out["details"] = {"selected": len(details_todo), "ok": 0, "none": 0, "failed": 0, "stopped": False,
                          "unresolved": []}
    if not (labs_todo or details_todo):
        return out
    pause = getattr(settings, "BATCH_LAB_WARM_PAUSE", DEFAULT_PAUSE) if pause is None else pause
    try:
        client = new_drops._client(location_slug)
    except Exception as exc:  # noqa: BLE001 - no credentials / config: one loud stop, not a crash
        out.update(stopped=True, unresolved=[b for b, _ in labs_todo], error=f"{type(exc).__name__}: {exc}")
        if "details" in out:
            out["details"]["unresolved"] = list(details_todo)
        logger.error("warm_batch_labs %s: no Dutchie client (%s); unresolved batches: %s; products: %s",
                     location_slug, out["error"], ", ".join(out["unresolved"]), ", ".join(details_todo))
        return out

    pace = {"called": False}  # shared: pacing is across the whole run, not per half
    labs_state = {"last": None, "streak": 0, "stopped": False}
    details_state = {"last": None, "streak": 0, "stopped": False}  # the details half has its OWN streak
    # Batches that already have a row are here because they are DUE (a re-check): the cached raw copy
    # (30 days) would answer without asking Dutchie, so it is dropped first.
    due = set(BatchLab.objects.filter(batch_id__in=[b for b, _ in labs_todo]).values_list("batch_id", flat=True))

    def before_call():
        if pace["called"] and pause:
            time.sleep(pause)  # between network calls only, never before the first
        pace["called"] = True

    def lab_step(item):
        bid, category = item
        key = f"newdrops:lab:{bid}"
        cached = cache.get(key)
        if bid in due or (isinstance(cached, dict) and is_empty(lab_from_data(cached, category))):
            cache.delete(key)  # a cached "no lab" / a copy older than the re-check: it cannot answer this
            cached = None
        if cached is None:
            before_call()
        return record(bid, new_drops.lab_for_batch(client, int(bid), strict=True), category, now)

    def detail_step(pid):
        before_call()
        return record_detail(pid, client.get_product_details(int(pid)), now)

    _drain(labs_todo, out, lab_step, labs_state, "b")
    if "details" in out:
        _drain(details_todo, out["details"], detail_step, details_state, "p")
        out["details"]["stopped"] = details_state["stopped"]
    out["stopped"] = labs_state["stopped"] or details_state["stopped"]
    if out["stopped"]:
        logger.error("warm_batch_labs %s: stopped after %d identical failures (labs: %s, details: %s); "
                     "unresolved batches: %s; unresolved products: %s", location_slug, STOP_AFTER_FAILURES,
                     labs_state["last"], details_state["last"], ", ".join(out["unresolved"]),
                     ", ".join(out.get("details", {}).get("unresolved", [])))
    return out
