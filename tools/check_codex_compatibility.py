"""Explicit isolated syntax checker. Never starts an agent or reads a login."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.codex_cli_compatibility import inspect, validate_output_path, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--scratch-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        validate_output_path(args.output, config_path=args.config, scratch_root=args.scratch_root)
        report = inspect(args.config, args.scratch_root)
        write_report(args.output, report, config_path=args.config, scratch_root=args.scratch_root)
    except (OSError, ValueError, RuntimeError):
        print("codex_compatibility_check_failed", file=sys.stderr)
        return 2
    print(report.status)
    return 0 if report.status == "CONFIGURATION_OBSERVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
