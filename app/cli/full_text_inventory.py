from __future__ import annotations

import argparse
import json

from app.services.full_text_inventory import inventory_full_text_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect local full-text artifacts without writing reviews.")
    parser.add_argument("--topic", default=None, help="Topic profile id, for example large-language-models.")
    parser.add_argument("--min-relevance", type=float, default=None, help="Minimum topic relevance score.")
    parser.add_argument("--only-with-text", action="store_true", help="Only show candidates with readable text/page/note artifacts.")
    parser.add_argument("--only-with-pdf", action="store_true", help="Only show candidates with local PDF artifacts.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidates to return.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    result = inventory_full_text_artifacts(
        topic_id=args.topic,
        min_relevance=args.min_relevance,
        only_with_text=args.only_with_text,
        only_with_pdf=args.only_with_pdf,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["items"]:
            title = item.get("title") or "(metadata unavailable)"
            score = float(item.get("topic_relevance_score") or 0)
            artifacts = ", ".join(item.get("artifact_types") or [])
            print(f"{item['paper_id']}\t{score:.1f}\t{artifacts}\t{title}")
        print(f"returned={result['returned_count']} total={result['total_artifact_candidates']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
