"""Package an existing structured capture for app import. Never sends a request."""
import argparse
from base64 import b64encode
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain.saved_analysis import MAX_BUNDLE_BYTES, validate_bundle  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--response', required=True, type=Path)
    parser.add_argument('--captured-at', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if any(path.stat().st_size > MAX_BUNDLE_BYTES for path in (args.plan, args.response)):
        parser.error('capture too large')
    plan = json.loads(args.plan.read_bytes())
    raw = args.response.read_bytes()
    bundle = dict(schema_version='SAVED_ANTHROPIC_ANALYSIS_V1', plan=plan, arm='structured',
                  response_b64=b64encode(raw).decode('ascii'), response_sha256=hashlib.sha256(raw).hexdigest(),
                  request_body_sha256=plan['arms']['structured']['body_sha256'], http_status=200,
                  captured_at=args.captured_at)
    validate_bundle(bundle)
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(bundle, target, ensure_ascii=False, separators=(',', ':'))
    print('Validated import bundle written; no Provider request or execution attestation.')


if __name__ == '__main__':
    main()
