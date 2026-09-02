"""``manage.py eval_answers`` — replay the golden set through every channel and print the table.

    python manage.py eval_answers                 # offline: text, playground, pos, storefront
    python manage.py eval_answers --live          # + voice (Gemini), web, web-fallback
    python manage.py eval_answers --channel text --id hours-yakima --dump

``--dump`` prints every answer verbatim (the Phase-0 baseline table); ``--out`` appends the report
to a markdown file so drift is visible over time.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from django.core.management.base import BaseCommand

from voice.evals import adapters, golden, run, score


class Command(BaseCommand):
    help = "Score every channel against kb/golden/answers.yaml"

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true", help="also run voice / web / web-fallback")
        parser.add_argument("--channel", action="append", default=[], help="restrict to channel(s)")
        parser.add_argument("--id", action="append", default=[], help="restrict to entry id(s)")
        parser.add_argument("--dump", action="store_true", help="print every answer verbatim")
        parser.add_argument("--out", default="", help="append the report to this markdown file")
        parser.add_argument("--real-budtender", action="store_true",
                            help="use the real budtender service instead of the fake catalog")
        parser.add_argument("--seed", action="store_true", help="seed the KB first (kb.seed.seed_all)")

    def handle(self, *args, **opts):
        if opts["seed"]:
            from kb.seed import seed_all

            seed_all()
        entries = golden.load()
        if opts["id"]:
            entries = [e for e in entries if e.id in set(opts["id"])]
        wanted = list(adapters.OFFLINE_CHANNELS) + (list(adapters.LIVE_CHANNELS) if opts["live"] else [])
        if opts["channel"]:
            wanted = [c for c in wanted if c in set(opts["channel"])] or list(opts["channel"])

        # The fake catalog is the default even for --live: the live question is whether the LLM
        # and the prompt agree with the text brain, not whether the budtender VPS is reachable
        # from this machine. --real-budtender opts into the real inventory.
        use_fake = not opts["real_budtender"]
        ctx = run.fake_budtender() if use_fake else _null()
        with ctx:
            results = run.run(entries, wanted)

        stamp = _dt.date.today().isoformat()
        title = f"eval_answers {stamp} ({'live' if opts['live'] else 'offline'}, " \
                f"{'fake' if use_fake else 'real'} budtender)"
        report = score.render(entries, results, title=title)
        if opts["dump"]:
            self.stdout.write(_dump(entries, results))
        self.stdout.write(report)
        if opts["out"]:
            path = Path(opts["out"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n" + report)
            self.stdout.write(f"appended to {path}")
        failed = [r for r in results if not r.passed]
        if failed:
            self.stdout.write(self.style.WARNING(f"{len(failed)}/{len(results)} checks failed"))


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _dump(entries, results) -> str:
    by_entry: dict[str, list] = {}
    for r in results:
        by_entry.setdefault(r.entry_id, []).append(r)
    lines = ["| id | channel | ok | answer | failures |", "|---|---|---|---|---|"]
    for e in entries:
        for r in by_entry.get(e.id, []):
            text = " ".join(r.answer.text.split()).replace("|", "\\|")[:300]
            fails = "; ".join(r.failures).replace("|", "\\|")[:200]
            lines.append(f"| {e.id} | {r.channel} | {'✓' if r.passed else '✗'} | {text} | {fails} |")
    return "\n".join(lines) + "\n\n"
