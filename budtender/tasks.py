"""
Celery sync + computation tasks. All are safe no-ops when Dutchie creds are
absent, so the service runs immediately and the website's local fallback covers
any gap until credentials are wired in.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils.text import slugify

from . import dutchie
from .models import STORES, CustomerProfile, Product, SuggestedProduct, SyncState

STORE_SLUGS = [s[0] for s in STORES]

# How stale the inventory may get before a fresh pull is forced. Suggestions are
# only ever drawn from in-stock products, so keeping this fresh is what prevents
# recommending something that has since sold out.
INVENTORY_MAX_AGE = timedelta(hours=24)


def inventory_is_stale(location_slug: str, max_age: timedelta = INVENTORY_MAX_AGE) -> bool:
    """True if `location_slug` has never synced or its last refresh is older than
    `max_age` (default 24h). Defensive: if the SyncState table isn't present yet
    (older DB not migrated), return False so we never crash or thrash — the
    frequent beat sync keeps stock fresh and the ranking still filters in-stock."""
    try:
        st = SyncState.objects.filter(location_slug=location_slug).first()
    except Exception:
        return False
    if not st or not st.last_synced_at:
        return True
    return (datetime.now(timezone.utc) - st.last_synced_at) >= max_age

# Dutchie omits unitCost on ~half of SKUs. Treating those as 100% margin
# (price − 0) falsely floats them to the top of every "highest margin" ranking.
# Instead, estimate margin from the store's own realized cost/price ratio:
# across all categories items WITH cost data cluster tightly at cost ≈ 0.26·price
# (margin ≈ 0.74·price), so we estimate the unknown ones at that fraction. This
# keeps real-margin items ranking on real numbers and estimated items ranking
# fairly against them within a price band, instead of dominating it.
EST_MARGIN_FRACTION = Decimal("0.72")  # slightly conservative vs the 0.74 observed


@shared_task
def sync_inventory(location_slug: str) -> int:
    rows = dutchie.fetch_inventory(location_slug)
    seen = set()
    for r in rows:
        price = Decimal(str(r.get("price", 0) or 0))
        cost = Decimal(str(r.get("cost", 0) or 0))
        sku = str(r.get("sku") or "")
        if not sku:
            continue
        seen.add(sku)
        # Known cost → true margin; unknown cost → estimate (never full price).
        margin = (price - cost) if cost > 0 else (price * EST_MARGIN_FRACTION)
        margin = max(margin, Decimal("0"))
        Product.objects.update_or_create(
            location_slug=location_slug,
            sku=sku,
            defaults={
                "slug": r.get("slug") or slugify(f"{r.get('name','')}-{sku}")[:200],
                "product_id": r.get("product_id", ""),
                "name": r.get("name", ""),
                "brand": r.get("brand", ""),
                "category": r.get("category", ""),
                "strain": r.get("strain", ""),
                "strain_type": r.get("strain_type", ""),
                "thc_percent": r.get("thc_percent"),
                "dominant_terpene": r.get("dominant_terpene", ""),
                "effects": r.get("effects", []),
                "flavors": r.get("flavors", []),
                "price": price,
                "price_was": Decimal(str(r["price_was"])) if r.get("price_was") else None,
                "cost": cost,
                "margin": margin,
                "quantity_on_hand": int(r.get("quantity_on_hand", 0) or 0),
                "availability": int(r.get("quantity_on_hand", 0) or 0) > 0,
                "image_url": r.get("image_url", ""),
                "unit_weight": r.get("unit_weight"),
                "potency_mg": r.get("potency_mg"),
            },
        )
    # Mark anything not in the latest pull as out of stock.
    if seen:
        Product.objects.filter(location_slug=location_slug).exclude(sku__in=seen).update(
            availability=False, quantity_on_hand=0
        )
        # Stamp the refresh time ONLY on a real pull (rows came back). A 0-row
        # result means creds/API are unavailable — leave the store marked stale so
        # the guard keeps retrying and admins can see it was never refreshed.
        # Defensive: tolerate the SyncState table not existing yet (unmigrated DB).
        try:
            SyncState.objects.update_or_create(
                location_slug=location_slug,
                defaults={"last_synced_at": datetime.now(timezone.utc), "item_count": len(seen)},
            )
        except Exception:
            pass

        # Precompute the questionnaire facets (subtypes/sizes/price-bands) into the
        # cache so the steps load INSTANTLY and one container serves many users
        # without re-scanning per request. Bump the version (invalidates the prior
        # snapshot) then warm the common queries. Best-effort — never fail a sync.
        try:
            from .facets import bump_version, warm
            bump_version(location_slug)
            warm(location_slug)
        except Exception:
            pass
    return len(seen)


@shared_task
def sync_inventory_all() -> dict:
    counts = {s: sync_inventory(s) for s in STORE_SLUGS}
    # Re-bucket products on every inventory refresh so margins/velocity stay current.
    classify_products_all()
    return counts


@shared_task
def ensure_inventory_fresh(max_age_hours: int = 24) -> dict:
    """Staleness guard: for each store, if its inventory hasn't been refreshed in
    the last `max_age_hours`, pull fresh now. Cheap no-op when already fresh — this
    is the 'check last refreshed, and if it's ≥24h old pull the fresh inventory'
    safety net that guarantees suggestions never come from stale stock."""
    max_age = timedelta(hours=max_age_hours)
    out: dict = {}
    refreshed = False
    for s in STORE_SLUGS:
        if inventory_is_stale(s, max_age):
            out[s] = sync_inventory(s)
            refreshed = True
        else:
            out[s] = "fresh"
    if refreshed:
        classify_products_all()
    return out


@shared_task
def sync_transactions(location_slug: str, days: int | None = None, full: bool = False) -> int:
    """Build customer purchase history + sales velocity from detailed transactions.

    `/reporting/transactions?includeDetail=true` gives each sale's line items
    (productId, qty, unitPrice, unitWeight, isReturned) + a `customerId`. We map
    customerId→phone (and customerId→name) via the customers list, and productId→Product
    (our synced, classified inventory) to attach brand/category/strain/strain_type/subcategory/bucket.

    HISTORY IS CUMULATIVE + EXACTLY-ONCE (P7): the pull always covers the velocity window (so
    velocity stays fresh), but a transaction is folded into customer history ONLY if it is strictly
    newer than the store's ``SyncState.last_tx_at`` watermark — so the recurring sync never
    re-counts a transaction it already folded (the old code re-folded the whole window every run and
    inflated counts). The watermark advances to the newest transaction seen. History is never
    pruned — folding only adds/updates per-product rows (``_fold_history``).
      • First run / ``full=True`` (watermark null): folds the FULL lookback window
        (``HHT_TX_LOOKBACK_DAYS``, default 1825 ≈ 5y) → a complete backfill of all history.
      • Steady state: folds only transactions after the watermark → just the new ones.

    VELOCITY (unchanged): recency-blended NET units/day 0.7·(28d)+0.3·(84d) over the pulled window,
    Redis ``vel:{loc}`` → classify_products → Product.velocity. Returns the # of customers folded.
    """
    now = datetime.now(timezone.utc)
    cutoff_28 = now - timedelta(days=28)
    cutoff_84 = now - timedelta(days=84)

    # Watermark: fold ONLY transactions newer than this (exactly-once). full/null → backfill window.
    st = SyncState.objects.filter(location_slug=location_slug).first()
    watermark = None if full else (st.last_tx_at if st else None)
    # The EXPLICIT full backfill pulls the absolute-oldest history Dutchie retains (≈20y default —
    # the API is paged in 31-day chunks, so this just makes more calls). The recurring beat stays
    # bounded to a recent window so it never accidentally does a 20-year pull; it relies on the
    # backfill command for the deep history, then folds only new transactions via the watermark.
    full_lookback = int(getattr(settings, "HHT_TX_LOOKBACK_DAYS", 7300))  # ≈20y — "absolute oldest"
    recent = int(getattr(settings, "HHT_TX_RECENT_DAYS", 120))           # beat / top-up window
    if full:
        window_days = int(days or full_lookback)        # explicit full backfill (oldest data)
    elif watermark is None:
        window_days = int(days or recent)               # first beat run — bounded, not the full pull
    else:
        window_days = max(recent, int(days or recent))  # steady state — covers the velocity window
    window_start = now - timedelta(days=window_days)

    u28: Counter = Counter()   # net units sold per sku, last 28 days
    u84: Counter = Counter()   # net units sold per sku, last 84 days
    # customerId → phone (prefer mobile) + customerId → display name.
    phone_by_id: dict[str, str] = {}
    name_by_id: dict[str, str] = {}
    for c in dutchie.get_customers(location_slug):
        cid = str(c.get("customerId") or c.get("id") or "")
        ph = c.get("cellPhone") or c.get("phone") or ""
        if cid and ph:
            phone_by_id[cid] = ph
        nm = (f"{c.get('firstName', '') or ''} {c.get('lastName', '') or ''}".strip()
              or c.get("name") or c.get("fullName") or "")
        if cid and nm:
            name_by_id[cid] = str(nm)[:120]
    # productId → Product (this location) for attribute + bucket join.
    prod_by_pid: dict[str, Product] = {}
    for p in Product.objects.filter(location_slug=location_slug).exclude(product_id=""):
        prod_by_pid[str(p.product_id)] = p

    rows = dutchie.get_transactions_detailed(
        location_slug, window_start.isoformat(), now.isoformat()
    )
    by_phone: dict[str, list[dict]] = defaultdict(list)
    name_by_phone: dict[str, str] = {}
    # Boundary dedup: ids already folded AT exactly the watermark second — so a same-second sale is
    # neither dropped (lossless) nor re-counted (exactly-once). max_tx_dt + new_boundary track the
    # new watermark and the ids sitting on it.
    old_boundary = set(st.last_tx_ids or []) if (st and not full) else set()
    max_tx_dt = watermark
    new_boundary = set(old_boundary)
    for tx in rows:
        if tx.get("isVoid") or tx.get("isReturn"):
            continue
        cid = str(tx.get("customerId") or "")
        phone = _normalize_phone(phone_by_id.get(cid, ""))
        if not phone:
            continue
        tx_id = str(tx.get("transactionId") or tx.get("id") or tx.get("transactionNumber") or "")
        bought_at = tx.get("transactionDate") or tx.get("lastModifiedDateUTC") or now.isoformat()
        bought_dt = dutchie._parse_iso(bought_at)
        # Exactly-once + lossless gate.
        if watermark is None:
            is_new = True
        elif bought_dt is None:
            is_new = False
        elif bought_dt > watermark:
            is_new = True
        elif bought_dt == watermark:
            # same-second as the watermark — fold only if this id wasn't already folded there.
            is_new = bool(tx_id) and tx_id not in old_boundary
        else:
            is_new = False
        if is_new and bought_dt is not None:
            if max_tx_dt is None or bought_dt > max_tx_dt:
                max_tx_dt, new_boundary = bought_dt, ({tx_id} if tx_id else set())
            elif bought_dt == max_tx_dt and tx_id:
                new_boundary.add(tx_id)
        # Always capture the latest Dutchie name for EVERY customer seen (even gated ones).
        if name_by_id.get(cid):
            name_by_phone[phone] = name_by_id[cid]
        for it in (tx.get("items") or []):
            if it.get("isReturned"):
                continue
            pid = str(it.get("productId") or "")
            if not pid:
                continue
            prod = prod_by_pid.get(pid)
            qty = float(it.get("quantity", 1) or 1)
            # Sales velocity: count NET units for real, sellable products over the WHOLE pulled
            # window (independent of the history watermark — velocity is a rolling recompute).
            if prod is not None and qty > 0 and bought_dt is not None and bought_dt >= cutoff_84:
                u84[prod.sku] += qty
                if bought_dt >= cutoff_28:
                    u28[prod.sku] += qty
            # History: fold only NEW transactions (exactly-once / no over-count).
            if is_new:
                by_phone[phone].append({
                    "product_id": pid,
                    "sku": prod.sku if prod else pid,
                    "brand": prod.brand if prod else "",
                    "category": prod.category if prod else "",
                    "subcategory": prod.subcategory if prod else "",
                    "strain": prod.strain if prod else "",
                    "strain_type": prod.strain_type if prod else "",
                    "bucket": prod.bucket if prod else "",
                    "dominant_terpene": prod.dominant_terpene if prod else "",
                    "price_z": float(prod.price_z) if prod else 0.0,
                    "qty": qty,
                    "unit_price": float(it.get("unitPrice") or 0),
                    "bought_at": bought_at,
                })
    for phone, lines in by_phone.items():
        _fold_history(phone, lines, name=name_by_phone.get(phone))

    # Refresh the name for customers SEEN but not folded this run (gated transactions), so a changed
    # Dutchie display name isn't stuck until their next brand-new purchase.
    for phone, nm in name_by_phone.items():
        if phone not in by_phone:
            CustomerProfile.objects.filter(phone=phone).exclude(name=nm).update(name=nm)

    # Advance the watermark + boundary id set so the next run folds only genuinely-new transactions.
    if max_tx_dt is not None:
        SyncState.objects.update_or_create(
            location_slug=location_slug,
            defaults={"last_tx_at": max_tx_dt, "last_tx_ids": list(new_boundary)},
        )

    # Recency-blended velocity (units/day): recent 28d weighted 0.7, trailing 84d
    # 0.3 — the dashboard's method, so picks track what's selling NOW. Stored in
    # Redis for classify_products to fold into Product.velocity (no schema change).
    skus = set(u28) | set(u84)
    velocity = {s: round(0.7 * (u28[s] / 28.0) + 0.3 * (u84[s] / 84.0), 4) for s in skus}
    cache.set(f"vel:{location_slug}", velocity, timeout=14 * 24 * 3600)
    classify_products(location_slug)   # propagate fresh velocity → Product.velocity + buckets
    return len(by_phone)


@shared_task
def sync_transactions_all() -> dict:
    return {s: sync_transactions(s) for s in STORE_SLUGS}


@shared_task
def recompute_affinity(phone: str) -> bool:
    """Turn purchase_history into the taste profile the ranking consumes:
    frequency-weighted affinity maps, a quality tier, a novelty score
    (habit↔explorer), and the core/traffic/profit bucket mix."""
    profile = CustomerProfile.objects.filter(phone=phone).first()
    if not profile:
        return False
    hist = profile.purchase_history or []
    if not hist:
        return False

    brand, cat, stype, sub, bucket = Counter(), Counter(), Counter(), Counter(), Counter()
    price_z_sum = price_z_n = 0.0
    total = 0
    for h in hist:
        n = int(h.get("times_bought", 1) or 1)
        total += n
        if h.get("brand"):
            brand[h["brand"]] += n
        if h.get("category"):
            cat[h["category"]] += n
        if h.get("strain_type"):
            stype[h["strain_type"]] += n
        if h.get("subcategory"):
            sub[h["subcategory"]] += n
        if h.get("bucket"):
            bucket[h["bucket"]] += n
        if h.get("price_z") is not None:
            price_z_sum += float(h.get("price_z") or 0) * n
            price_z_n += n

    profile.brand_affinity = _normalize_counter(brand)
    profile.category_affinity = _normalize_counter(cat)
    profile.strain_type_affinity = _normalize_counter(stype)
    profile.subcategory_affinity = _normalize_counter(sub)
    profile.bucket_mix = _normalize_counter(bucket)
    profile.total_orders = total

    # Terpene affinity: purchase_history doesn't carry terpene, so join each
    # purchased SKU → its product's dominant terpene. This ACTIVATES the terpene
    # term in ranking._affinity_score (the field was defined but never populated).
    terp: Counter = Counter()
    sku_list = [h.get("sku") for h in hist if h.get("sku")]
    terp_by_sku: dict[str, str] = {}
    if sku_list:
        for sku_, dt in (Product.objects.filter(sku__in=sku_list)
                         .exclude(dominant_terpene="")
                         .values_list("sku", "dominant_terpene")):
            terp_by_sku.setdefault(sku_, dt)
    for h in hist:
        # Prefer the terpene captured at sync time (permanent); else join the
        # current product. (Dutchie sends 0% terpene for this account today, so
        # this stays empty until that data appears — then it lights up for free.)
        dt = h.get("dominant_terpene") or terp_by_sku.get(h.get("sku"))
        if dt:
            terp[dt] += int(h.get("times_bought", 1) or 1)
    profile.terpene_affinity = _normalize_counter(terp)

    # Most-recent purchase timestamp (feeds the recency boost in ranking).
    last_dts = [h.get("last_bought_at") for h in hist if h.get("last_bought_at")]
    if last_dts:
        def _aware(d):
            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        try:
            profile.last_purchase_at = max(_aware(d) for d in last_dts)
        except (ValueError, TypeError):
            pass

    # Quality tier from mean price-z of what they buy (vs that item's peers).
    mean_pz = (price_z_sum / price_z_n) if price_z_n else 0.0
    profile.price_tier = "top" if mean_pz >= 0.4 else ("value" if mean_pz <= -0.4 else "mid")

    # Novelty: distinct products ÷ total purchases. ~1 = always something new
    # (explorer); low = repeats the same items (creature of habit).
    distinct = len(hist)
    profile.novelty_score = round(min(distinct / total, 1.0), 3) if total else 0.0

    profile.computed_at = datetime.now(timezone.utc)
    profile.save(update_fields=[
        "brand_affinity", "category_affinity", "strain_type_affinity",
        "subcategory_affinity", "terpene_affinity", "bucket_mix", "price_tier",
        "novelty_score", "total_orders", "last_purchase_at", "computed_at",
    ])
    return True


@shared_task
def build_copurchase(location_slug: str) -> int:
    """Build the 'frequently bought together' matrices into Redis from customer
    purchase histories, weighted by CONFIDENCE — each customer's lifetime set is a
    basket and confidence(A→B) = P(A∧B)/P(A) is literally 'what fraction of people
    who bought A also bought B', the right measure for an UPSELL ('what people
    usually pair'). (Lift is for surprising associations — it would filter out a
    universally-popular complement like a cart, which is exactly what we DO want to
    upsell — so we use confidence, not lift.) A ≥ MIN_CO co-buyer gate kills noise:
      • pair:{loc}:{sku}      -> {sku: confidence}            exact-SKU
      • pairattr:{loc}:{attr} -> {category|size: confidence}  durable (survives SKU rotation)
    A dict is written for EVERY anchor (empty when nothing qualifies) so a rebuild
    leaves no stale weights — the historical universe only grows between runs.
    pair_for restricts candidates to COMPLEMENT categories, so same-category
    confidences in the matrix are simply never queried."""
    from .pairing import pair_attr_key
    MIN_CO = 3  # ignore pairs fewer than 3 customers co-bought (kills noise)

    sku_single: Counter = Counter()
    attr_single: Counter = Counter()
    sku_pair: dict[str, Counter] = defaultdict(Counter)
    attr_pair: dict[str, Counter] = defaultdict(Counter)
    for profile in CustomerProfile.objects.all().iterator():
        hist = profile.purchase_history or []
        skus = sorted({h.get("sku") for h in hist if h.get("sku")})
        attrs = sorted({pair_attr_key(h.get("category"), h.get("subcategory"))
                        for h in hist if h.get("category")})
        for s in skus:
            sku_single[s] += 1
        for i, a in enumerate(skus):
            for b in skus[i + 1:]:
                sku_pair[a][b] += 1
                sku_pair[b][a] += 1
        for a in attrs:
            attr_single[a] += 1
        for i, a in enumerate(attrs):
            for b in attrs[i + 1:]:
                attr_pair[a][b] += 1
                attr_pair[b][a] += 1

    def emit(prefix: str, single: Counter, pairmat: dict[str, Counter]) -> None:
        for a, ca in single.items():
            ca = ca or 1
            weights = {b: round(co / ca, 4)            # confidence(a→b) ∈ [0,1]
                       for b, co in pairmat.get(a, {}).items() if co >= MIN_CO}
            cache.set(f"{prefix}:{location_slug}:{a}", weights, timeout=None)

    emit("pair", sku_single, sku_pair)
    emit("pairattr", attr_single, attr_pair)
    return len(attr_single)


@shared_task
def build_copurchase_all() -> dict:
    return {s: build_copurchase(s) for s in STORE_SLUGS}


# ── Profit-strategy classification (subsystem 1) ─────────────────────────────
MIN_GROUP = 8        # min items in a (category×subcategory) group before fallback
PROFIT_MARGIN_Z = 0.5
TRAFFIC_MARGIN_Z = -0.5
TRAFFIC_PRICE_Z = -0.25


def _percentile(vals: list[float], pct: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@shared_task
def classify_products(location_slug: str) -> int:
    """Bucket every in-stock product (core/traffic/profit) on a bell curve within
    its (category × subcategory) peer group. Manual buckets are preserved."""
    from statistics import mean, pstdev

    from .ranking import size_label

    # Sales velocity = recency-blended NET units/day per sku, computed in
    # sync_transactions from REAL sales only (returns/voids/trade-samples excluded)
    # and cached in Redis. Empty until the first transactions sync → traffic
    # detection simply won't fire and items fall to core (safe default).
    vel: dict = cache.get(f"vel:{location_slug}") or {}

    prods = list(Product.objects.filter(location_slug=location_slug, availability=True))
    for p in prods:
        p.subcategory = size_label(p.unit_weight, p.potency_mg, p.category)
        price = float(p.price) or 0.0
        p.margin_pct = (float(p.margin) / price) if price > 0 else 0.0
        p.velocity = float(vel.get(p.sku, 0))

    # Group, merging sparse (category×subcategory) groups into category-only.
    raw: dict[tuple, list] = defaultdict(list)
    for p in prods:
        raw[(p.category, p.subcategory)].append(p)
    groups: dict[tuple, list] = defaultdict(list)
    for (cat, sub), items in raw.items():
        key = (cat, sub) if len(items) >= MIN_GROUP else (cat, "*")
        groups[key].extend(items)

    now = datetime.now(timezone.utc)
    for items in groups.values():
        margins = [p.margin_pct for p in items]
        prices = [float(p.price) for p in items]
        gps = [float(p.margin) for p in items]
        vels = [p.velocity for p in items]
        m_mean, m_sd = (mean(margins), pstdev(margins) or 1.0) if margins else (0, 1)
        p_mean, p_sd = (mean(prices), pstdev(prices) or 1.0) if prices else (0, 1)
        gp90 = _percentile(gps, 90)   # only the genuine top-GP items, not just "expensive"
        vel60 = _percentile(vels, 60)
        for p in items:
            p.margin_z = (p.margin_pct - m_mean) / m_sd
            p.price_z = (float(p.price) - p_mean) / p_sd
            p.classified_at = now
            if p.bucket_source == "manual":
                continue
            # Profit-driver: clearly above-peer margin %, or a true top-GP$ item.
            if p.margin_z >= PROFIT_MARGIN_Z or float(p.margin) >= gp90:
                p.bucket = "profit"
            # Traffic-driver: cheap + low-margin. Velocity sharpens this once
            # transaction data is aligned (subsystem 2); until then (vel60==0) we
            # classify on price+margin alone so the bucket isn't empty.
            elif (p.margin_z <= TRAFFIC_MARGIN_Z and p.price_z <= TRAFFIC_PRICE_Z
                  and (vel60 == 0 or p.velocity >= vel60)):
                p.bucket = "traffic"
            else:
                p.bucket = "core"

    Product.objects.bulk_update(
        prods,
        ["subcategory", "margin_pct", "velocity", "margin_z", "price_z", "bucket", "classified_at"],
        batch_size=500,
    )
    return len(prods)


@shared_task
def classify_products_all() -> dict:
    return {s: classify_products(s) for s in STORE_SLUGS}


# ── helpers ──────────────────────────────────────────────────────────────────
def _normalize_phone(raw: str) -> str:
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    # Not a usable US phone (empty, too short, or absurdly long — some Dutchie
    # records carry junk). Drop it: a profile key must fit phone varchar(20), and
    # a malformed number can't reliably identify a returning customer anyway.
    return ""


def _fold_history(phone: str, lines: list[dict], name: str | None = None) -> None:
    profile, _ = CustomerProfile.objects.get_or_create(phone=phone)
    key = lambda h: str(h.get("product_id") or h.get("sku") or "")
    agg: dict[str, dict] = {key(h): h for h in (profile.purchase_history or []) if key(h)}
    for ln in lines:
        k = str(ln.get("product_id") or ln.get("sku") or "")
        if not k:
            continue
        entry = agg.get(k) or {
            "product_id": ln.get("product_id", ""), "sku": ln.get("sku", ""),
            "brand": ln.get("brand", ""), "category": ln.get("category", ""),
            "subcategory": ln.get("subcategory", ""), "strain": ln.get("strain", ""),
            "strain_type": ln.get("strain_type", ""), "bucket": ln.get("bucket", ""),
            "price_z": ln.get("price_z", 0.0), "times_bought": 0, "last_bought_at": None,
        }
        entry["times_bought"] = int(entry.get("times_bought", 0)) + 1
        entry["last_bought_at"] = ln["bought_at"]
        entry["last_price"] = ln.get("unit_price", entry.get("last_price"))
        # refresh joined attributes if we now resolved the product
        for f in ("brand", "category", "subcategory", "strain", "strain_type", "bucket", "price_z"):
            if ln.get(f):
                entry[f] = ln[f]
        agg[k] = entry
    profile.purchase_history = list(agg.values())
    fields = ["purchase_history"]
    if name and name != profile.name:
        profile.name = name  # latest known display name from Dutchie (staff browse)
        fields.append("name")
    profile.save(update_fields=fields)

    # Conversion attribution: a previously-suggested SKU that the customer now
    # bought is marked accepted=True. Powers "did our suggestion convert?".
    bought_skus = [e.get("sku") for e in agg.values() if e.get("sku")]
    if bought_skus:
        SuggestedProduct.objects.filter(
            customer=profile, sku__in=bought_skus, accepted__isnull=True
        ).update(accepted=True)

    recompute_affinity(phone)


def _normalize_counter(counter: Counter) -> dict:
    total = sum(counter.values()) or 1
    return {k: round(v / total, 4) for k, v in counter.items()}
