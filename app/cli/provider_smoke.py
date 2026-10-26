from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.services import provider_cli_smoke


def _load_profile(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile JSON must be an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run safe provider CLI readiness/version/tiny-smoke checks.")
    parser.add_argument("--provider", default=None, help="Provider alias or provider profile id.")
    parser.add_argument("--all", action="store_true", help="Check built-in provider CLI aliases.")
    parser.add_argument("--profile-json", default=None, help="Optional local profile JSON for tests/operator proof.")
    parser.add_argument("--dry-run", action="store_true", help="Readiness only; this is the default without smoke flags.")
    parser.add_argument("--version-smoke", action="store_true", help="Run configured version/help command only.")
    parser.add_argument("--allow-real-provider-smoke", action="store_true", help="Request tiny prompt smoke; also requires ALLOW_REAL_PROVIDER_SMOKE=1.")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="Override provider command timeout.")
    parser.add_argument("--max-output-chars", type=int, default=None, help="Override sanitized stdout/stderr excerpt length.")
    args = parser.parse_args(argv)

    if args.all and args.profile_json:
        parser.error("--profile-json cannot be combined with --all")
    if not args.all and not args.provider:
        parser.error("--provider is required unless --all is used")

    if args.all:
        payload: dict[str, Any] = {
            "items": [
                provider_cli_smoke.build_provider_cli_smoke_result(
                    provider,
                    version_smoke=bool(args.version_smoke),
                    allow_real_provider_smoke=bool(args.allow_real_provider_smoke),
                    timeout_seconds=args.timeout_seconds,
                    max_output_chars=args.max_output_chars,
                )
                for provider in provider_cli_smoke.PROVIDER_CHOICES
            ]
        }
    else:
        payload = provider_cli_smoke.build_provider_cli_smoke_result(
            str(args.provider),
            profile=_load_profile(args.profile_json),
            version_smoke=bool(args.version_smoke),
            allow_real_provider_smoke=bool(args.allow_real_provider_smoke),
            timeout_seconds=args.timeout_seconds,
            max_output_chars=args.max_output_chars,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
