from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.services.deployment_smoke import deployment_smoke_failed, render_deployment_smoke_markdown, run_deployment_smoke


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("Expected true or false.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run staging or local deployment smoke checks for the XRML web app and MCP surface.")
    parser.add_argument("--base-url", required=True, help="Public or local base URL for the deployed backend.")
    parser.add_argument("--web-base-path", default="/app", help="Production SPA base path. Defaults to /app.")
    parser.add_argument("--bearer-token", help="Optional bearer token for one authenticated read-only MCP check.")
    parser.add_argument("--run-id", help="Run id for the optional authenticated read-only MCP check.")
    parser.add_argument("--json", action="store_true", help="Emit the full machine-readable smoke result as JSON.")
    parser.add_argument("--markdown-summary", help="Optional path to write a markdown summary report.")
    parser.add_argument(
        "--fail-on-warning",
        type=_parse_bool,
        default=False,
        help="Exit non-zero when warnings are present. Defaults to false.",
    )
    parser.add_argument("--retry-attempts", type=int, default=1, help="Number of smoke attempts before failing. Defaults to 1.")
    parser.add_argument("--retry-delay-seconds", type=int, default=15, help="Delay between retries in seconds. Defaults to 15.")
    args = parser.parse_args(argv)

    result = None
    for attempt in range(1, max(args.retry_attempts, 1) + 1):
        result = run_deployment_smoke(
            base_url=args.base_url,
            web_base_path=args.web_base_path,
            bearer_token=args.bearer_token,
            run_id=args.run_id,
        )
        if not deployment_smoke_failed(result, fail_on_warning=args.fail_on_warning):
            break
        if attempt < max(args.retry_attempts, 1):
            time.sleep(max(args.retry_delay_seconds, 0))

    assert result is not None
    payload = result.to_dict()

    if args.markdown_summary:
        summary_path = Path(args.markdown_summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(render_deployment_smoke_markdown(result), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"base_url: {payload['base_url']}")
        print(f"web_app_ok: {payload['web_app_ok']}")
        print(f"mcp_initialize_ok: {payload['mcp_initialize_ok']}")
        print(f"tools_list_ok: {payload['tools_list_ok']}")
        print(f"metadata_ok: {payload['metadata_ok']}")
        print(f"unauthorized_challenge_ok: {payload['unauthorized_challenge_ok']}")
        print(f"authenticated_read_ok: {payload['authenticated_read_ok']}")
        if payload["checked_tool_names"]:
            print(f"checked_tool_names: {', '.join(payload['checked_tool_names'])}")
        for warning in payload["warnings"]:
            print(f"warning: {warning}")
        for failure in payload["failures"]:
            print(f"failure: {failure}")

    return 0 if not deployment_smoke_failed(result, fail_on_warning=args.fail_on_warning) else 1


if __name__ == "__main__":
    raise SystemExit(main())
