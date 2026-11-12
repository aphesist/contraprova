"""Command line: ``contraprova verify|tasks|cost|report <log>`` and ``contraprova init-claude``."""

from __future__ import annotations

import argparse
import sys

from .events import ChainError
from .log import EventLog
from .pricing import DEFAULT_TABLE, PRICE_TABLES, cost_by
from .projections import InvalidTransition, arm_usage_records, project_tasks, usage_records


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
            p.add_argument("--lanes", action="store_true", help="Contract Lanes report: primary family La vs "
                           "La-shadow only (exact McNemar), La vs D by paired bootstrap, clash counts, planner share")
        if name == "cost":
            p.add_argument("--by", default="role", help="usage field to group by (role, model, arm, task_id); "
                           "with arm, fork spend an arm did not ship is grouped as '<arm>:measurement'")
    ic = sub.add_parser("init-claude", help="install the cascade agents into ./.claude (or ~/.claude with --user)")
    ic.add_argument("--user", action="store_true",
                    help="install into $CLAUDE_CONFIG_DIR or ~/.claude for all projects, not ./.claude")
    ic.add_argument("--force", action="store_true", help="overwrite files that already exist")
    ic.add_argument("--no-gate", action="store_true", help="skip the gate hook script and the orchestrator's hooks")
    ic.add_argument("--source", help="plugin directory to copy from (default: config/claude-code in this checkout)")
    args = ap.parse_args(argv)
    if args.cmd == "init-claude":
        return _init_claude(args)
    log = EventLog(args.log)

    try:
        if args.cmd == "verify":
            print(f"ok: {log.verify()} events, chain intact, replay clean")
        elif args.cmd == "tasks":
            for t in project_tasks(log.events()).values():
                print(f"{t.task_id}\t{t.status}\tattempts={t.attempts}\t{t.title}")
        elif args.cmd == "cost":
            evs = log.events()
            records = arm_usage_records(evs) if args.by == "arm" else usage_records(evs)
            totals = cost_by(records, PRICE_TABLES[args.prices], args.by)
            for k, usd in sorted(totals.items(), key=lambda kv: -kv[1]):
                print(f"{k}\t${usd:.6f}")
            print(f"total\t${sum(totals.values()):.6f}\t(prices: {args.prices})")
        elif args.cmd == "report":
            from .analysis import report_markdown

            if args.lanes:
                from .analysis import lanes_report_markdown

                print(lanes_report_markdown(log.events(), args.prices, seed=args.seed))
            else:
                print(report_markdown(log.events(), args.prices, seed=args.seed))
    except (ChainError, InvalidTransition) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


def _init_claude(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .claude_init import default_source, init_claude, user_dir

    source = Path(args.source) if args.source else default_source()
    if source is None or not (source / "agents").is_dir():
        print("FAILED: cannot find the agent files. Run this from a contraprova checkout (pip install -e .), "
              "pass --source config/claude-code, or install the plugin instead:\n"
              "  claude plugin install contraprova-cascade --marketplace aphesist/contraprova", file=sys.stderr)
        return 1
    target = user_dir() if args.user else Path.cwd() / ".claude"
    results = init_claude(target, source, project=not args.user, force=args.force, gate=not args.no_gate)
    for path, action in results:
        print(f"{action}\t{path}")
    if any(action == "kept" for _, action in results):
        print("some files already existed and were kept; pass --force to overwrite them")
    print("\nStart the cascade (pick the orchestrator's model and effort; Opus at medium or high is recommended):\n"
          "  export CONTRAPROVA_TEST_CMD='pytest -q'   # optional: the gate runs it after each agent\n"
          "  claude --agent orchestrator --model opus --effort high")
    if not args.user and not args.no_gate:
        print("Claude Code runs the orchestrator's gate hooks only after you accept the workspace trust dialog "
              "for this folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
