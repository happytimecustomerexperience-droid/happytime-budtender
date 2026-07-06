"""Smoke #2 — live write path on a TEST register. USE A TEST GUEST + cheap item.

    python scripts/pos_smoke.py yakima --acct <ACCT_ID> \
        --product <PRODUCT_ID> --batch <BATCH_ID> --serial <SERIAL_NO> \
        --price <PRICE> --avail <ALLOTMENT> --desc "<PRODUCT NAME>" \
        [--schedule <SCHEDULE_ID> --shipment <SHIPMENT_ID>]

If --schedule/--shipment are omitted, start_visit() mints them (path confirmed live).
Verify the cart in the Dutchie POS UI afterward, then VOID it. Writes a real order.
"""

import argparse

from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("--acct", type=int, required=True)
    ap.add_argument("--product", type=int, required=True)
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--price", type=float, default=1.0)
    ap.add_argument("--avail", type=float, default=1.0)
    ap.add_argument("--desc", default="SMOKE TEST ITEM")
    ap.add_argument("--schedule", type=int)
    ap.add_argument("--shipment", type=int)
    ap.add_argument("--status", default="Ready for pickup")
    a = ap.parse_args()

    client = PosRegisterClient(get_store(a.store))
    item = {
        "ProductId": a.product, "BatchId": a.batch, "SerialNo": a.serial,
        "AvailOz": a.avail, "RecUnitPrice": a.price, "UnitPrice": a.price,
        "ProductDesc": a.desc, "CannbisProduct": "Yes",
    }
    result = client.submit_cart(
        a.acct, [item], schedule_id=a.schedule, shipment_id=a.shipment,
        final_status=a.status,
    )
    print("shipment:", result["shipment_id"], "schedule:", result["schedule_id"])
    print("saved:", result["saved"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
