from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_NOTES_ROOT
from app.repositories.files import read_jsonl, safe_filename, write_text
from app.services.research_domain import (
    DomainContextSource,
    ResearchDomainContext,
    build_research_domain_context,
    markdown_inline,
)


def _load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_items(triage_path: Path, inbox_path: Path) -> tuple[list[dict[str, Any]], Path]:
    items = read_jsonl(triage_path)
    if items:
        return items, triage_path
    return read_jsonl(inbox_path), inbox_path


def generate_notes(
    *,
    items: list[dict[str, Any]],
    template_path: Path,
    out_dir: Path,
    top_n: int,
    source_label: str,
    domain_context: ResearchDomainContext | None = None,
) -> list[str]:
    template = _load_template(template_path)
    resolved_context = domain_context or build_research_domain_context(
        source=DomainContextSource.LEGACY_NEUTRAL
    )
    domain_label = markdown_inline(resolved_context.display_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    for item in items[:top_n]:
        paper_id = item.get("paper_id") or safe_filename(item.get("title", "paper"))
        output_path = out_dir / f"{safe_filename(paper_id)}.md"
        filled = template.format(
            title=item.get("title", "").strip(),
            paper_id=paper_id,
            published=item.get("published", ""),
            updated=item.get("updated", ""),
            authors=", ".join(item.get("authors", [])),
            entry_url=item.get("entry_url") or item.get("entry_id", ""),
            pdf_url=item.get("pdf_url", ""),
            doi=item.get("doi", ""),
            abstract=(item.get("abstract", "") or "").strip(),
            tags=", ".join(
                (item.get("triage", {}).get("hits_primary", []) or item.get("keyword_hits", {}).get("primary", []))[:12]
            )
            or domain_label,
            citation_list=f"- arXiv: {paper_id}\n- URL: {item.get('entry_url') or item.get('entry_id', '')}",
            domain_section_title=f"{domain_label} Relevance",
        )
        header = f"<!-- generated_at_utc: {datetime.now(timezone.utc).isoformat()} | source: {source_label} -->\n\n"
        write_text(output_path, header + filled)
        created.append(str(output_path))

    return created


def summarize_from_paths(
    *,
    triage_path: Path,
    inbox_path: Path,
    template_path: Path,
    out_dir: Path,
    top_n: int,
    mirror_legacy: bool = False,
    domain_context: ResearchDomainContext | None = None,
    source_label: str | None = None,
) -> dict[str, Any]:
    items, source_path = _load_items(triage_path, inbox_path)
    if not items:
        return {"status": "empty", "message": "No items found in triage/inbox.", "created": []}

    created = generate_notes(
        items=items,
        template_path=template_path,
        out_dir=out_dir,
        top_n=top_n,
        source_label=source_label or str(source_path),
        domain_context=domain_context,
    )

    legacy_created: list[str] = []
    legacy_mirror_errors: list[str] = []
    if mirror_legacy:
        try:
            legacy_created = generate_notes(
                items=items,
                template_path=template_path,
                out_dir=LEGACY_NOTES_ROOT / "papers",
                top_n=top_n,
                source_label=source_label or str(source_path),
                domain_context=domain_context,
            )
        except OSError as exc:
            legacy_mirror_errors.append(str(exc))

    return {
        "status": "ok",
        "source": source_label or str(source_path),
        "created": created,
        "legacy_created": legacy_created,
        "legacy_mirror_errors": legacy_mirror_errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--inbox", default="data/queues/inbox.jsonl")
    parser.add_argument("--template", default="templates/paper_summary_template.md")
    parser.add_argument("--out_dir", default="notes/papers")
    args = parser.parse_args(argv)

    result = summarize_from_paths(
        triage_path=Path(args.triage),
        inbox_path=Path(args.inbox),
        template_path=Path(args.template),
        out_dir=Path(args.out_dir),
        top_n=args.top,
        mirror_legacy=False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
