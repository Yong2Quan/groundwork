"""Command-line entry point.

    triage analyze              corpus + retrieval + validity checks, no model
    triage run                  run the sweep
    triage report               render tables from a completed sweep
    triage economics            cost per 100 incidents, and the break-even
    triage compare              two or more sweeps side by side by model
    triage serve                run the MCP server on stdio
    triage inspect              print the MCP Inspector command
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze, economics, report, store
from .configs import ARMS, mcp_config
from .evaluate import RESULTS_DIR, Sweep, run_sweep
from .providers import PROVIDERS


def _latest_run() -> Path | None:
    runs = sorted(RESULTS_DIR.glob("*.json"))
    return runs[-1] if runs else None


def cmd_analyze(args: argparse.Namespace) -> int:
    text = analyze.render()
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0 if "_Status: PASS_" in text else 1


def cmd_run(args: argparse.Namespace) -> int:
    incident_ids = store.incident_ids()
    if args.limit:
        incident_ids = incident_ids[: args.limit]
    if args.incidents:
        incident_ids = args.incidents

    arms = args.arms or list(ARMS)
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"unknown arm(s): {unknown}; choose from {list(ARMS)}",
              file=sys.stderr)
        return 2

    sweep, path = run_sweep(
        provider_name=args.provider,
        model=args.model,
        arms=arms,
        incident_ids=incident_ids,
        effort=args.effort,
        out_path=Path(args.out) if args.out else None,
        resume=args.resume,
    )
    print()
    print(report.render(sweep, path))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.run) if args.run else _latest_run()
    if not path or not path.exists():
        print("no run found; pass a path or run `triage run` first",
              file=sys.stderr)
        return 2
    text = report.render(Sweep.load(path), path)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


def cmd_economics(args: argparse.Namespace) -> int:
    path = Path(args.run) if args.run else _latest_run()
    if not path or not path.exists():
        print("no run found; pass a path or run `triage run` first",
              file=sys.stderr)
        return 2
    assumptions = economics.Assumptions(
        escalation_minutes=args.escalation_minutes,
        misdirection_minutes=args.misdirection_minutes,
        hourly_rate=args.hourly_rate,
    )
    text = economics.render(Sweep.load(path), assumptions)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    paths = [Path(r) for r in args.runs]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"no such run(s): {[p.as_posix() for p in missing]}",
              file=sys.stderr)
        return 2
    sweeps = [(p, Sweep.load(p)) for p in paths]
    text = report.compare(sweeps)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"\nwritten to {args.out}")
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from .mcp_server import main
    main()
    return 0


def cmd_inspect(_args: argparse.Namespace) -> int:
    server = next(iter(mcp_config()["mcpServers"].values()))
    cmd = " ".join([server["command"], *server["args"]])
    print("Launch the MCP Inspector against this server with:\n")
    print(f'  npx -y @modelcontextprotocol/inspector "{server["command"]}" '
          f'{" ".join(server["args"])}\n')
    print("Set this environment variable first so the server can import the "
          "package:\n")
    print(f'  PYTHONPATH={server["env"]["PYTHONPATH"]}\n')
    print(f"Equivalent direct command: {cmd}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Incident-triage agent with an eval harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser(
        "analyze", help="corpus, retrieval and validity checks (no model)")
    p_analyze.add_argument("--out", help="also write the markdown here")
    p_analyze.set_defaults(func=cmd_analyze)

    p_run = sub.add_parser("run", help="run the eval sweep")
    p_run.add_argument("--provider", default="claude_cli", choices=PROVIDERS,
                       help="default: claude_cli (no API key needed)")
    p_run.add_argument("--model", default=None,
                       help="provider-specific; e.g. sonnet / opus / haiku "
                            "for claude_cli")
    p_run.add_argument("--effort", default=None,
                       choices=["low", "medium", "high", "xhigh", "max"])
    p_run.add_argument("--arms", nargs="*", default=None,
                       help=f"subset of {list(ARMS)}")
    p_run.add_argument("--incidents", nargs="*", default=None,
                       help="specific incident ids")
    p_run.add_argument("--limit", type=int, default=None,
                       help="first N incidents only (smoke tests)")
    p_run.add_argument("--out", default=None, help="write the run here")
    p_run.add_argument("--resume", action="store_true",
                       help="skip cells already present in --out")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="render tables from a run")
    p_report.add_argument("run", nargs="?", help="path to a run JSON")
    p_report.add_argument("--out", help="also write the markdown here")
    p_report.set_defaults(func=cmd_report)

    p_econ = sub.add_parser(
        "economics", help="cost per 100 incidents, and the break-even")
    p_econ.add_argument("run", nargs="?", help="path to a run JSON")
    p_econ.add_argument("--escalation-minutes", type=float,
                        default=economics.DEFAULT_ESCALATION_MINUTES,
                        help="engineer time when the tool abstains")
    p_econ.add_argument("--misdirection-minutes", type=float,
                        default=economics.DEFAULT_MISDIRECTION_MINUTES,
                        help="engineer time when the tool is wrong")
    p_econ.add_argument("--hourly-rate", type=float,
                        default=economics.DEFAULT_HOURLY_RATE,
                        help="fully loaded engineer cost per hour")
    p_econ.add_argument("--out", help="also write the markdown here")
    p_econ.set_defaults(func=cmd_economics)

    p_cmp = sub.add_parser(
        "compare", help="put two or more sweeps side by side by model")
    p_cmp.add_argument("runs", nargs="+", help="paths to run JSON files")
    p_cmp.add_argument("--out", help="also write the markdown here")
    p_cmp.set_defaults(func=cmd_compare)

    p_serve = sub.add_parser("serve", help="run the MCP server on stdio")
    p_serve.set_defaults(func=cmd_serve)

    p_inspect = sub.add_parser(
        "inspect", help="print the MCP Inspector command for this server")
    p_inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
