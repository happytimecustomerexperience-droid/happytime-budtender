"""List Dutchie registers per store (live) — also proves the real creds log in.

    python manage.py discover_registers            # list only
    python manage.py discover_registers --write     # fill register_id=first register for stores with 0

Writes back into stores.json (preserving creds) only with --write, and never
overwrites a store that already has a non-zero register_id.
"""

import json

from django.core.management.base import BaseCommand

from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import _path, load_stores


class Command(BaseCommand):
    help = "List Dutchie registers per store; optionally fill missing register_id."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true",
                            help="set register_id to the first register for stores with 0")

    def handle(self, *args, **opts):
        stores = load_stores()
        if not stores:
            self.stderr.write("no stores.json")
            return
        picks = {}
        for name, store in stores.items():
            try:
                regs = PosRegisterClient(store).get_registers()
            except Exception as exc:
                self.stderr.write(f"{name}: LOGIN/FETCH FAILED — {exc}")
                continue
            self.stdout.write(self.style.SUCCESS(
                f"{name} (loc {store.loc_id}): {len(regs)} register(s)"))
            for r in regs:
                self.stdout.write(f"    id={r.get('id')}  {r.get('TerminalName')}  room={r.get('RoomNo')}")
            if regs:
                picks[name] = regs[0].get("id")

        if not opts["write"]:
            self.stdout.write("\n(dry run — re-run with --write to fill missing register_id)")
            return

        raw = json.loads(_path().read_text(encoding="utf-8"))
        changed = []
        for name, cfg in raw.items():
            if not cfg.get("register_id") and picks.get(name):
                cfg["register_id"] = int(picks[name])
                changed.append(f"{name}->{cfg['register_id']}")
        _path().write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS("wrote register_id: " + (", ".join(changed) or "none")))
