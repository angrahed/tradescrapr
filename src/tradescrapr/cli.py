"""Command-line entry point: plan / run / status / monitor / retry-dead / refs / da."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .errors import ConfigError, TradescraprError

log = logging.getLogger("tradescrapr")


def setup_logging(logs_dir: Path, verbose: bool, console_output: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if console_output:  # suppressed for the full-screen dashboard
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        root.addHandler(console)

    filelog = RotatingFileHandler(
        logs_dir / "tradescrapr.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    filelog.setLevel(logging.DEBUG)
    filelog.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(filelog)
    # requests/urllib3 are chatty at DEBUG
    logging.getLogger("urllib3").setLevel(logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradescrapr",
        description="UN Comtrade scraper: targeted long-run jobs and a monthly release monitor.",
    )
    p.add_argument("--version", action="version", version=f"tradescrapr {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging on console")
    p.add_argument("--root", type=Path, default=None, help="project root (default: auto-detect)")
    p.add_argument("--tier", choices=["free", "premium"], default=None, help="override tier")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="expand a YAML job spec into a job + tasks")
    sp.add_argument("spec", type=Path, help="path to job spec YAML")
    sp.add_argument("--dry-run", action="store_true", help="print what would be created")

    sp = sub.add_parser("run", help="run the worker (resume-by-default)")
    sp.add_argument("--job", default=None, help="restrict to one job id")
    sp.add_argument("--max-calls", type=int, default=None, help="stop after N API calls")
    sp.add_argument(
        "--stop-after", type=float, default=None, metavar="HOURS", help="stop after this many hours"
    )
    sp.add_argument(
        "--no-wait-quota",
        action="store_true",
        help="exit instead of sleeping when the daily quota is exhausted",
    )

    sp = sub.add_parser("status", help="job progress, quota usage, ETA")
    sp.add_argument("--job", default=None, help="restrict to one job id")
    sp.add_argument(
        "--verbose", dest="status_verbose", action="store_true", help="list dead tasks with errors"
    )

    sp = sub.add_parser("monitor", help="scrape newly released data (for cron)")
    sp.add_argument("--dry-run", action="store_true", help="print the release diff and exit")
    sp.add_argument(
        "--wait-quota",
        action="store_true",
        help="sleep through quota exhaustion instead of exiting",
    )

    sp = sub.add_parser("retry-dead", help="revive dead tasks (reset attempts)")
    sp.add_argument("--job", default=None, help="restrict to one job id")
    sp.add_argument("--task", default=None, help="a single task id")

    sp = sub.add_parser("refs", help="reference data management")
    refsub = sp.add_subparsers(dest="refs_command", required=True)
    refsub.add_parser("refresh", help="force re-download of reference files")

    sp = sub.add_parser(
        "ui", aliases=["dashboard"], help="live terminal dashboard (runs beside a worker)"
    )
    sp.add_argument(
        "--refresh",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="repaint interval (default 2s)",
    )
    sp.add_argument("--once", action="store_true", help="print one snapshot and exit")

    sp = sub.add_parser("da", help="debug: print data availability (free endpoint)")
    sp.add_argument("--type", dest="type_code", default="C", choices=["C", "S"])
    sp.add_argument("--freq", default="M", choices=["A", "M"])
    sp.add_argument("--classification", default="HS")
    sp.add_argument("--period", default=None, help="filter, e.g. 202405")
    sp.add_argument("--reporter", default=None, help="filter, ISO3 or numeric")

    return p


def _load(args: argparse.Namespace) -> Config:
    overrides = {"tier": args.tier}
    cfg = load_config(root=args.root, cli_overrides=overrides)
    cfg.paths.ensure()
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _load(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    setup_logging(
        cfg.paths.logs,
        args.verbose,
        console_output=args.command not in ("ui", "dashboard"),
    )
    log.debug("root=%s tier=%s", cfg.paths.root, cfg.tier)

    try:
        if args.command == "plan":
            from .jobs.targeted import cmd_plan

            return cmd_plan(cfg, args.spec, dry_run=args.dry_run)
        if args.command == "run":
            from .worker import cmd_run

            return cmd_run(
                cfg,
                job_id=args.job,
                max_calls=args.max_calls,
                stop_after_hours=args.stop_after,
                wait_quota=not args.no_wait_quota,
            )
        if args.command == "status":
            from .status import cmd_status

            return cmd_status(cfg, job_id=args.job, verbose=args.status_verbose)
        if args.command == "monitor":
            from .jobs.monitor import cmd_monitor

            return cmd_monitor(cfg, dry_run=args.dry_run, wait_quota=args.wait_quota)
        if args.command == "retry-dead":
            from .status import cmd_retry_dead

            return cmd_retry_dead(cfg, job_id=args.job, task_id=args.task)
        if args.command == "refs":
            from .refs import cmd_refs_refresh

            return cmd_refs_refresh(cfg)
        if args.command in ("ui", "dashboard"):
            from .ui import cmd_ui

            return cmd_ui(cfg, refresh=args.refresh, once=args.once)
        if args.command == "da":
            from .availability import cmd_da

            return cmd_da(
                cfg,
                type_code=args.type_code,
                freq=args.freq,
                classification=args.classification,
                period=args.period,
                reporter=args.reporter,
            )
        raise AssertionError(f"unhandled command {args.command}")
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except TradescraprError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
