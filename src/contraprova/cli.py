"""Command line: ``contraprova verify|tasks|cost <log>``."""

from __future__ import annotations

import argparse
import sys

from .events import ChainError
from .log import EventLog
from .pricing import DEFAULT_TABLE, PRICE_TABLES, cost_by
from .projections import InvalidTransition, project_tasks, usage_records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contraprova")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (
        ("verify", "check the hash chain and replay every transition"),
        ("tasks", "print the task projection"),
        ("cost", "re-price recorded usage"),
        ("report", "study report in Markdown, every number with its interval"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("log")
        if name in ("cost", "report"):
            p.add_argument("--prices", default=DEFAULT_TABLE, choices=sorted(PRICE_TABLES))
        if name == "report":
            p.add_argument("--seed", type=int, default=0)
        if name == "cost":
            p.add_argument("--by", default="role", help="usage field to group by (role, model, arm, task_id)")
    args = ap.parse_args(argv)
    log = EventLog(args.log)

    try:
        if args.cmd == "verify":
            print(f"ok: {log.verify()} events, chain intact, replay clean")
        elif args.cmd == "tasks":
            for t in project_tasks(log.events()).values():
                print(f"{t.task_id}\t{t.status}\tattempts={t.attempts}\t{t.title}")
        elif args.cmd == "cost":
            totals = cost_by(usage_records(log.events()), PRICE_TABLES[args.prices], args.by)
            for k, usd in sorted(totals.items(), key=lambda kv: -kv[1]):
                print(f"{k}\t${usd:.6f}")
            print(f"total\t${sum(totals.values()):.6f}\t(prices: {args.prices})")
        elif args.cmd == "report":
            from .analysis import report_markdown

            print(report_markdown(log.events(), args.prices, seed=args.seed))
    except (ChainError, InvalidTransition) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
