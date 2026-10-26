from __future__ import annotations

import argparse
import json

from app.services import oauth_readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run safe OAuth/MCP read-only readiness, mock, and live-gated proof checks.")
    parser.add_argument("--dry-run", action="store_true", help="Readiness only; this is the default without proof flags.")
    parser.add_argument("--metadata", action="store_true", help="Validate local OAuth/MCP metadata and read-only route inventory.")
    parser.add_argument("--mock-exchange", action="store_true", help="Run local mock exchange validation only; not live connector proof.")
    parser.add_argument(
        "--allow-live-oauth-smoke",
        action="store_true",
        help="Request live read-only connector smoke; also requires ALLOW_LIVE_OAUTH_SMOKE=1 and public HTTPS/IdP prerequisites.",
    )
    args = parser.parse_args(argv)

    payload = oauth_readiness.build_oauth_readonly_proof_result(
        metadata=bool(args.metadata),
        mock_exchange=bool(args.mock_exchange),
        allow_live_oauth_smoke=bool(args.allow_live_oauth_smoke),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
