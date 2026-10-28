from __future__ import annotations

import argparse
import json
import signal
from typing import Sequence

from app.services import topic_schedules
from app.services.topic_scheduler import TopicScheduleWorker


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AI Research Agent topic scheduler worker.")
    parser.add_argument("--mode", choices=["external", "embedded", "disabled"], default="external")
    parser.add_argument("--tick-seconds", type=float, default=None)
    parser.add_argument("--once", action="store_true", help="Run one scheduler cycle and exit.")
    parser.add_argument("--recover-orphans", action="store_true", help="Recover orphaned runs during a one-shot cycle.")
    parser.add_argument("--status", action="store_true", help="Print scheduler runtime status as JSON and exit.")
    parser.add_argument("--check", action="store_true", help="Run scheduler runtime preflight checks and exit.")
    parser.add_argument("--lock-check", action="store_true", help="Print schedule store lock preflight JSON and exit.")
    parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Safely recover a stale schedule store lock during --lock-check.",
    )
    parser.add_argument("--runtime-mode", default=None, help="Override scheduler runtime mode for --status/--check.")
    return parser


def _print_json(payload: dict) -> None:
    # Redirected Windows stdout may use a legacy code page. JSON escapes preserve
    # every Unicode status value without depending on the operator's encoding.
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.lock_check:
        payload = topic_schedules.schedule_store_lock_preflight(recover_stale=bool(args.recover_stale_lock))
        _print_json(payload)
        return 0 if payload.get("ok") else 2
    if args.check:
        payload = topic_schedules.scheduler_preflight_check(runtime_mode=args.runtime_mode, expected_mode=args.mode)
        _print_json(payload)
        return 0 if payload.get("ok") else 2
    if args.status:
        _print_json(topic_schedules.get_scheduler_runtime_status(expected_mode=args.mode, runtime_mode=args.runtime_mode))
        return 0

    worker = TopicScheduleWorker(mode=args.mode, tick_seconds=args.tick_seconds)
    if args.once:
        _print_json(worker.run_cycle_now(recover_orphans=bool(args.recover_orphans)))
        return 0

    def _stop(_signum, _frame) -> None:
        worker.request_stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    worker.run_foreground()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
