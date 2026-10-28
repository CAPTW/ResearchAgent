from __future__ import annotations

import argparse
import json
from typing import Sequence

from app.ops.production_activation import build_production_activation_result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check production activation readiness without exposing secret values.")
    parser.add_argument("--require-oidc", action="store_true", help="Fail unless OIDC production auth is configured and safe.")
    parser.add_argument("--require-smtp", action="store_true", help="Fail unless SMTP email env is configured and valid.")
    parser.add_argument("--strict", action="store_true", help="Treat not_ready deployment status as a blocking activation failure.")
    parser.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as activation failures.")
    parser.add_argument("--check-oidc-discovery", action="store_true", help="Fetch OIDC discovery metadata and validate required fields.")
    parser.add_argument("--check-smtp-connectivity", action="store_true", help="Connect/login/noop against SMTP without sending email.")
    parser.add_argument("--profile", default=None, help="Operator-facing profile label to include in output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = build_production_activation_result(
        require_oidc=bool(args.require_oidc),
        require_smtp=bool(args.require_smtp),
        strict=bool(args.strict),
        fail_on_warnings=bool(args.fail_on_warnings),
        check_oidc_discovery=bool(args.check_oidc_discovery),
        check_smtp_connectivity=bool(args.check_smtp_connectivity),
        profile=args.profile,
    )
    # JSON escapes preserve Unicode even on legacy Windows redirected stdout.
    print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
