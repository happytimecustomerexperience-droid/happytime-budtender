"""PosRegisterClient — budtender write path against ash.pos.dutchie.com.

All shapes below are CONFIRMED from a live HAR capture (2026-06-26). The full flow:

  checkin_guest(acct)            -> Data[0].ShipmentId + Data[0].ScanResult (== ScheduleId)
  guest_details(acct)            -> Allotment (== AvailOz), ShipmentId, ScheduleId
  select_guest_to_register(...)  -> attach guest to this register
  product_search()               -> ENTIRE live inventory (no query; filter locally)
  price_check(serial)            -> confirm live price for a package
  add_item_to_cart(...)          -> add one line (AvailOz = guest Allotment)
  update_transaction_status(...) -> save ("Ready for pickup"); TransId == ShipmentId

Session block lives in dutchie/session.py. All bodies = {**args, **session_block}.
"""

from __future__ import annotations

import logging
import time

from .session import DutchieUnavailable, PosClient

logger = logging.getLogger(__name__)


def _cents(value) -> float | None:
    """float(value) rounded to cents, or None if it isn't a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def parse_cart_totals(resp: dict) -> dict | None:
    """/api/v2/cart/load_v2 -> the breakdown the register prints, or None.

    Shape CONFIRMED from dutchie/fixtures/cart_load_v2.json:

        SubTotal 8.25 / TotalDiscountAndLoyalty 0 / Tax 3.75 / RoundingAmount 0
        / GrandTotalRounded 12 (what the till actually charges)

    Defensive like parse_price_check: every field is probed by name and a miss stays
    None — a missing total is NEVER coerced to 0, because 0 is a plausible-looking
    wrong answer to show a customer. Returns None (never raises) when the payload
    isn't a usable cart, so a totals lookup can't blow up a page render.
    """
    if not isinstance(resp, dict) or resp.get("Result") is False:
        return None
    d = resp.get("Data")
    if isinstance(d, list):
        d = d[0] if d else None
    if not isinstance(d, dict):
        return None

    def pick(keys):
        for k in keys:
            v = _cents(d.get(k))
            if v is not None:
                return v
        return None

    cart = d.get("Cart")
    lines = []
    for row in cart if isinstance(cart, list) else []:
        if not isinstance(row, dict):
            continue
        lines.append({
            "product": row.get("Product") or row.get("ProductDesc") or "",
            "product_id": row.get("ProductId"),
            "serial_no": row.get("SerialNo"),
            "quantity": _cents(row.get("PricedQuantity")),
            "unit_price_formatted": row.get("UnitPriceFormatted"),
            "total_formatted": row.get("TotalFormatted"),
            "total": _cents(row.get("TotalCost")),
            "tax": _cents(row.get("TaxAmt")),
            "discount": _cents(row.get("DiscountAmt")),
            "loyalty": _cents(row.get("LoyaltyAmt")),
        })

    items = d.get("TotalItems")
    try:
        item_count = int(items) if items is not None else None
    except (TypeError, ValueError):
        item_count = None

    return {
        "subtotal": pick(["SubTotal", "Subtotal", "SubTotalAmount"]),
        "discounts_and_loyalty": pick(["TotalDiscountAndLoyalty", "TotalDiscount",
                                       "DiscountAndLoyalty"]),
        "tax": pick(["Tax", "TaxAmount", "TotalTax"]),
        "rounding": pick(["RoundingAmount", "Rounding"]),
        # GrandTotalRounded first: it is the amount the register charges.
        "total": pick(["GrandTotalRounded", "GrandTotal", "OrderTotal", "Total"]),
        "item_count": item_count,
        "lines": lines,
    }


def map_product_row(row: dict) -> dict:
    """A product_SearchV2 Data row -> the `item` dict add_item_to_cart needs."""
    return {
        "ProductId": row.get("ProductId"),
        "BatchId": row.get("BatchId"),
        "SerialNo": row.get("SerialNo") or row.get("SourceSerialNumber"),
        "ProductDesc": row.get("ProductDesc") or row.get("ProductDescription") or "",
        "UnitPrice": row.get("UnitPrice") or 0,
        "RecUnitPrice": row.get("RecUnitPrice") or row.get("UnitPrice") or 0,
        "CannbisProduct": "Yes" if row.get("CannabisInventory") == "Yes" else "No",
        "Available": row.get("TotalAvailable"),
        "Category": row.get("ProductCategory") or row.get("MasterCategory") or "",
        "Brand": row.get("BrandName") or "",
    }


class PosRegisterClient(PosClient):
    base_origin = "https://ash.pos.dutchie.com"

    def _login_base(self) -> str:
        # POS register: EmployeeLogin is served on the POS host itself (HAR-confirmed).
        return self.store.pos_base_url or self.store.base_url

    # ── guest lifecycle ──────────────────────────────────────────────────────
    def checkin_guest(self, acct_id: int, room_id: str = "", mj_state_id: str = "") -> dict:
        """POST /api/v2/guest/checkin_guest. Register is 0 here (set later via select)."""
        body = {"AcctId": int(acct_id), "MJStateIDNo": mj_state_id or "", "Register": 0,
                "RoomId": room_id or "", **self.session_block(with_register=False)}
        return self.post("/api/v2/guest/checkin_guest", body)

    def guest_details(self, acct_id: int) -> dict:
        """POST /api/v2/guest/details -> {Allotment, ShipmentId, ScheduleId, ...}."""
        body = {"Guest_id": int(acct_id), **self.session_block(with_register=False)}
        return self.post("/api/v2/guest/details", body)

    def select_guest_to_register(self, guest_id: int, schedule_id, shipment_id: int) -> dict:
        body = {
            "Guest_id": int(guest_id),
            "RegisterId": int(self.store.register_id),
            "ScheduleId": int(schedule_id),
            "ShipmentId": int(shipment_id),
            **self.session_block(with_register=False),
        }
        return self.post("/api/v2/guest/Select_Guest_To_Register", body)

    # ── inventory ────────────────────────────────────────────────────────────
    def product_search(self) -> list[dict]:
        """POST /api/v2/product/product_SearchV2 — returns the FULL live inventory list
        (the endpoint takes no query; filter client-side with find_products)."""
        body = {"Register": int(self.store.register_id), **self.session_block(with_register=False)}
        data = self.post("/api/v2/product/product_SearchV2", body)
        out = data.get("Data")
        return out if isinstance(out, list) else []

    def find_products(self, query: str, limit: int = 40) -> list[dict]:
        """Local filter over product_search() by description/strain/brand/category."""
        q = (query or "").strip().lower()
        rows = self.product_search()
        if not q:
            return rows[:limit]
        hits = []
        for r in rows:
            hay = " ".join(str(r.get(k, "")) for k in
                           ("ProductDescription", "ProductDesc", "Strain", "BrandName",
                            "ProductCategory", "MasterCategory", "ProductNo", "SerialNo")).lower()
            if q in hay:
                hits.append(r)
                if len(hits) >= limit:
                    break
        return hits

    def get_registers(self) -> list[dict]:
        """POST /api/posv3/registers/get -> [{id, TerminalName, RoomId, RoomNo, ...}]
        for this store's LocId. Also doubles as a read-only login smoke."""
        data = self.post("/api/posv3/registers/get", self.session_block(with_register=False))
        out = data.get("Data")
        return out if isinstance(out, list) else []

    def price_check(self, serial_no: str) -> dict:
        body = {"PackageSerialNumber": str(serial_no), **self.session_block(with_register=False)}
        return self.post("/api/v2/inventory/price-check", body)

    @staticmethod
    def parse_price_check(resp: dict) -> dict:
        """Normalize /inventory/price-check -> {price, rec_price, discount, available, ok}.

        THIS RETURNED None FOR EVERY FIELD UNTIL THE SHAPE WAS ACTUALLY CAPTURED. The
        old version probed eight plausible names for the price and six for availability,
        and the live payload (`dutchie/fixtures/`, and the price-check entries in the
        2026-08-06 capture) contains none of them:

            {"Result": true, "Data": {"ProductName": "...", "FlowerEquivalent": "0.00000g",
             "ProductGrams": "28.35 g", "Quantity": 0, "Price": "$ 12.00",
             "PricingTier": {...}}}

        `Price` is a FORMATTED STRING — `float("$ 12.00")` raises, the probe swallowed the
        ValueError and moved on, and the function returned all-None with `ok: True`. So
        every caller believed the check had succeeded and quietly kept its cached price.
        The live price confirmation never once worked.

        `Quantity` is deliberately NOT mapped to `available`. In the capture, a price-check
        returning `Quantity: 0` was immediately followed by a successful add of that item
        to the cart at $12.00 — so whatever it counts, it is not "units you may sell", and
        `pos/views.py` blocks the add when `available <= 0`. Mapping it would refuse real
        sales. TotalAvailable from product_SearchV2 remains the stock figure.

        The old speculative names are kept as fallbacks: this endpoint plainly returns more
        fields for a discounted item than the two rows we captured, and a name that shows up
        later should work without another archaeology session.
        """
        d = (resp or {}).get("Data") if isinstance(resp, dict) else None
        if isinstance(d, list):
            d = d[0] if d else {}
        if not isinstance(d, dict):
            d = resp if isinstance(resp, dict) else {}

        def num(v):
            """Money as Dutchie sends it: 12.0, "12.00", or "$ 12.00"."""
            if v is None or isinstance(v, bool):
                return None
            if isinstance(v, (int, float)):
                return float(v)
            cleaned = str(v).replace("$", "").replace(",", "").strip()
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None

        def pick(keys):
            for k in keys:
                got = num(d.get(k))
                if got is not None:
                    return got
            return None

        # "Price" first: it is the one field the live payload actually has.
        price = pick(["Price", "DiscountedUnitPrice", "DiscountedPrice",
                      "UnitPriceAfterDiscount", "FinalUnitPrice", "FinalPrice",
                      "NetUnitPrice", "UnitPrice"])
        rec = pick(["RecUnitPrice", "OriginalUnitPrice", "OriginalPrice", "RegularPrice",
                    "ListPrice"])
        disc = pick(["DiscountAmount", "TotalDiscount", "Discount", "DiscountTotal"])
        avail = pick(["TotalAvailable", "AvailableQuantity", "QtyAvailable", "Available",
                      "QuantityAvailable", "OnHand"])
        if disc is None and rec is not None and price is not None and rec > price:
            disc = round(rec - price, 2)
        ok = isinstance(resp, dict) and resp.get("Result") is not False
        return {"price": price, "rec_price": rec, "discount": disc, "available": avail, "ok": ok}

    # ── cart ─────────────────────────────────────────────────────────────────
    def add_item_to_cart(self, acct_id: int, shipment_id: int, item: dict, avail_oz: float,
                         *, auto_discount: bool = True, auto_price: bool = True) -> dict:
        """POST /api/v2/cart/add_item_to_shopping_cart. avail_oz = guest Allotment.

        auto_discount/auto_price default True so Dutchie applies EVERY configured
        auto-discount, promo and pricing rule to the line server-side (authoritative at
        write time) — the budtender flow should never under-discount a real order."""
        body = {
            "AcctId": int(acct_id),
            "AvailOz": float(avail_oz or 0),
            "BatchId": item.get("BatchId"),
            "CannbisProduct": item.get("CannbisProduct", "Yes"),
            "Cnt": int(item.get("Cnt", 1)),
            "DefaultLabelId": item.get("DefaultLabelId"),
            "DefaultUnitId": item.get("DefaultUnitId", 1),
            "Grouping": item.get("Grouping", "No"),
            "LoyaltyAsDiscount": True,
            "ProductDesc": item.get("ProductDesc", ""),
            "ProductId": item.get("ProductId"),
            "QuantityItem": True,
            "RecUnitPrice": item.get("RecUnitPrice", item.get("UnitPrice", 0)),
            "Register": int(self.store.register_id),
            "RunAutoDiscount": bool(auto_discount),
            "RunAutoPrice": bool(auto_price),
            "SerialNo": item.get("SerialNo"),
            "ShipmentId": int(shipment_id),
            "UnitPrice": item.get("UnitPrice", 0),
            "UsingDaysSupply": False,
            "Weight": item.get("Weight", 0),
            **self.session_block(with_register=False),
        }
        return self.post("/api/v2/cart/add_item_to_shopping_cart", body)

    def load_cart(self, acct_id: int, shipment_id: int, register: int | None = None) -> dict:
        """POST /api/v2/cart/load_v2 -> the raw cart (lines + register totals).

        Body mirrors the capture exactly; Timestamp is epoch MILLISECONDS there.
        Feed the result to parse_cart_totals().
        """
        body = {
            "AcctId": int(acct_id),
            "Register": int(register if register is not None else self.store.register_id),
            "ShipmentId": int(shipment_id),
            "Timestamp": int(time.time() * 1000),
            **self.session_block(with_register=False),
        }
        return self.post("/api/v2/cart/load_v2", body)

    def cart_totals(self, acct_id: int, shipment_id: int, register: int | None = None) -> dict | None:
        """load_cart + parse_cart_totals. Returns None on any failure — a totals
        lookup must never raise into a page render."""
        try:
            return parse_cart_totals(self.load_cart(acct_id, shipment_id, register))
        except Exception as exc:  # noqa: BLE001 — display-only path, degrade quietly
            logger.warning("cart_totals(acct=%s ship=%s) failed: %s", acct_id, shipment_id, exc)
            return None

    def update_transaction_status(self, trans_id: int, status: str = "Ready for pickup") -> dict:
        body = {"TransId": int(trans_id), "TransactionStatus": status,
                **self.session_block(with_register=False)}
        return self.post("/api/posv3/maintenance/UpdateTransactionStatus", body)

    # ── guest lookup by phone/name (CONFIRMED endpoint) ──────────────────────
    # Dutchie answers a GENUINE no-match with Result=false and "No matching guests
    # found" — not an empty Data list. `PosClient.post` turns every Result=false into
    # DutchieUnavailable, which is right for the rest of the API and wrong here: it
    # made "nobody has this number" indistinguishable from "the register is down", at
    # the source, for every caller. Confirmed live 2026-08-10.
    #
    # Matched narrowly on purpose. The same endpoint refuses a too-broad query with
    # "Please Narrow Your Search", which is NOT a no-match and must keep raising —
    # swallowing every Result=false here would report "no account" for a search that
    # was never actually run.
    _NO_MATCH = "no matching guest"

    def guest_search(self, query: str) -> dict:
        """POST /api/v2/guest/checkin_search_by_string {SearchString} (phone or name)
        -> Data:[{Guest_id, Name, PhoneNo, PatientType, DOB, LastTransaction, ...}].

        An empty Data list means Dutchie looked and found nobody. Anything it could
        not answer still raises."""
        body = {"SearchString": (query or "").strip(), **self.session_block(with_register=False)}
        try:
            data = self.post("/api/v2/guest/checkin_search_by_string", body)
        except DutchieUnavailable as exc:
            if self._NO_MATCH in str(exc).lower():
                logger.info("guest_search(%r) -> 0 match(es)", query)
                return {"Result": True, "Data": []}
            raise
        n = len((data or {}).get("Data") or []) if isinstance(data, dict) else 0
        logger.info("guest_search(%r) -> %d match(es)", query, n)
        return data

    @staticmethod
    def _dob_iso(dob: str) -> str:
        """'YYYY-MM-DD' -> the ISO form Dutchie's create expects."""
        d = (dob or "").strip()[:10]
        return f"{d}T08:00:00.000Z" if len(d) == 10 else ""

    def create_guest(self, *, first_name: str, last_name: str, dob: str, phone: str,
                     email: str = "", mj_state_id: str = "", dl_id: str = "",
                     address: str = "", address2: str = "", city: str = "", state: str = "",
                     postal_code: str = "", customer_type: int = 2) -> int | None:
        """POST /api/v2/guest/create (Recreational by default) -> new Guest_id (AcctId).
        Mirrors the captured create payload exactly."""
        body = {
            "FirstName": first_name or "", "LastName": last_name or "", "status": "Active",
            "street": address or "", "street2": address2 or "", "city": city or "",
            "state": state or "", "postal_code": postal_code or "",
            "MJStateIDNo": mj_state_id or "", "MJStateIDStartDate": "", "MMJIDState": "",
            "ExpirationDate": "", "CertificationCollectionDate": "", "CertificationExpirationDate": "",
            "CustomerTypeId": int(customer_type), "PhoneNo": phone or "", "CellPhone": "",
            "country_code": "US", "DLExpirationDate": "", "DriversLicenseId": dl_id or "",
            "DOB": self._dob_iso(dob), "email": email or "",
            **self.session_block(with_register=False),
        }
        data = self.post("/api/v2/guest/create", body)
        d = (data or {}).get("Data") or []
        row = d[0] if isinstance(d, list) and d else {}
        gid = row.get("Guest_id")
        logger.info("create_guest -> %s", gid)
        return int(gid) if gid else None

    def guest_details_light(self, acct_id: int) -> dict:
        """POST /api/v2/guest/details-light {CustomerId} -> light profile."""
        body = {"CustomerId": int(acct_id), **self.session_block(with_register=False)}
        return self.post("/api/v2/guest/details-light", body)

    # ── orchestration ────────────────────────────────────────────────────────
    @staticmethod
    def _ids_from_checkin(checkin: dict) -> tuple[int | None, int | None]:
        d = (checkin or {}).get("Data") or []
        row = d[0] if isinstance(d, list) and d else {}
        ship = row.get("ShipmentId")
        sched = row.get("ScanResult") or row.get("ScheduleId")
        return (int(ship) if ship else None, int(sched) if sched else None)

    def submit_cart(self, acct_id: int, items: list[dict], *, room_id: str = "",
                    final_status: str = "Ready for pickup",
                    auto_discount: bool = True, auto_price: bool = True) -> dict:
        """Full flow: checkin -> details(allotment) -> select -> add* -> save.

        `items` are `map_product_row` dicts (each carries ProductId/BatchId/SerialNo/price).
        auto_discount/auto_price flow to every add_item so the order gets all discounts.
        """
        checkin = self.checkin_guest(acct_id, room_id=room_id)
        ship, sched = self._ids_from_checkin(checkin)

        allot = 0.0
        details = self.guest_details(acct_id)
        gd = (details or {}).get("Data") or {}
        if isinstance(gd, dict):
            allot = float(gd.get("Allotment") or 0)
            ship = ship or (int(gd["ShipmentId"]) if gd.get("ShipmentId") else None)
            sched = sched or (int(gd["ScheduleId"]) if gd.get("ScheduleId") else None)

        if not ship or not sched:
            raise RuntimeError(f"no ShipmentId/ScheduleId (ship={ship} sched={sched}) — checkin failed?")

        self.select_guest_to_register(acct_id, sched, ship)
        added = [self.add_item_to_cart(acct_id, ship, it, allot,
                                       auto_discount=auto_discount, auto_price=auto_price)
                 for it in items]
        saved = self.update_transaction_status(ship, final_status)
        return {"shipment_id": ship, "schedule_id": sched, "allotment": allot,
                "added": added, "saved": saved}
