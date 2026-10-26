from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services import ai_review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic local fixture command for provider execution smoke tests.")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--topic")
    args = parser.parse_args(argv)

    packet_path = Path(args.packet)
    output_path = Path(args.output)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    draft = ai_review.generate_fixture_ai_review_draft(packet)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
