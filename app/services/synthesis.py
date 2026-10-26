from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_NOTES_ROOT
from app.repositories.files import read_jsonl, write_text
from app.services.research_domain import (
    DomainContextSource,
    ResearchDomainContext,
    build_research_domain_context,
    markdown_inline,
)


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def iso_week_tag(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def synthesize_weekly(
    *,
    triage_path: Path,
    out_path: Path,
    top_n: int,
    domain_context: ResearchDomainContext,
    mirror_legacy: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    items = read_jsonl(triage_path)
    if not items:
        return {"status": "empty", "message": "triage.jsonl not found or empty"}

    now = generated_at or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    recent: list[tuple[datetime, dict[str, Any]]] = []
    for item in items:
        timestamp = _parse_dt(item.get("published", "")) or _parse_dt(item.get("updated", ""))
        if timestamp and timestamp >= cutoff:
            recent.append((timestamp, item))
    recent.sort(key=lambda pair: (pair[1].get("score", 0), pair[0]), reverse=True)
    picked = [item for _, item in recent[:top_n]]

    tag_counts: dict[str, int] = {}
    for item in picked:
        hits = item.get("triage", {}).get("hits_primary") or item.get("keyword_hits", {}).get("primary") or []
        for hit in hits[:20]:
            tag_counts[hit] = tag_counts.get(hit, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda pair: pair[1], reverse=True)[:12]
    week = iso_week_tag(now)
    domain_label = markdown_inline(domain_context.display_name)

    lines: list[str] = []
    lines.append(f"# Weekly Research Brief — {domain_label} — {week}\n")
    lines.append(f"- Generated (UTC): {now.strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"- Papers considered (last 7 days): {len(recent)}\n")
    lines.append(f"- Selected top: {len(picked)}\n")
    lines.append(
        f"- Domain context source: {domain_context.source.value}\n"
    )
    lines.append("\n## Top papers (short)\n")
    for index, item in enumerate(picked, start=1):
        paper_id = item.get("paper_id", "")
        title = item.get("title", "").strip()
        score = item.get("score", 0)
        url = item.get("entry_url") or item.get("entry_id", "")
        published = item.get("published", "")
        lines.append(
            f"{index}. **[{score:>3}]** {title}\n"
            f"   - id: {paper_id}\n"
            f"   - published: {published}\n"
            f"   - url: {url}\n"
        )

    lines.append("\n## Trend signals (heuristic)\n")
    if top_tags:
        for tag, count in top_tags:
            lines.append(f"- {tag}: {count}")
    else:
        lines.append("- (no tag signals)")

    lines.append("\n## Research gaps / open questions (draft)\n")
    if domain_context.research_questions:
        for question in domain_context.research_questions:
            lines.append(f"- {markdown_inline(question)}")
    elif picked and top_tags:
        observed_tags = ", ".join(markdown_inline(tag) for tag, _ in top_tags[:4])
        lines.append(
            f"- 현재 선택 문헌에서 관찰된 신호({observed_tags})가 더 넓은 {domain_label} 문헌에서도 반복되는지 추가 검증이 필요합니다."
        )
        lines.append(
            "- 논문별 데이터, 방법, 평가 조건의 차이가 관찰된 결과에 미치는 영향을 비교할 근거가 아직 제한적입니다."
        )
    elif picked:
        lines.append(
            f"- 선택된 {domain_label} 문헌에서 반복되는 keyword signal이 충분하지 않아 구체적인 연구 공백을 단정할 수 없습니다."
        )
    else:
        lines.append(
            "- 최근 7일 범위에 선택된 문헌이 없어 evidence-backed 연구 공백을 도출하지 않았습니다."
        )

    lines.append("\n## Proposed Experiment Scaffolds (Not Executed)\n")
    lines.append(
        "- Classification: research design proposal only; `execution_status=not_executed`, "
        "`executable=false`, and `runner_available=false`.\n"
    )
    if picked:
        lines.append(
            "1) 제안 가설: 최상위 문헌과 연결된 근거를 검토한 뒤 검증 가능한 질문을 별도로 정의할 수 있습니다.\n"
        )
        lines.append(
            "2) 제안 방법: 데이터 또는 연구 자료, 표본 규모, 평가 지표, baseline을 근거에 따라 선택하는 "
            "설계 절차를 작성할 수 있습니다.\n"
        )
        lines.append(
            f"3) {domain_label} 범위에서 추가 evidence가 필요한 질문을 다음 수집 주기의 우선순위로 기록할 수 있습니다.\n"
        )
        lines.append(
            "- Unresolved inputs: dataset/study materials, sample size, evaluation metric, "
            "baseline, execution environment, and implementation.\n"
        )
    else:
        lines.append(
            "1) 근거가 부족하므로 구체적인 가설, 방법, 데이터, 지표 또는 결과를 제안하지 않습니다.\n"
        )
        lines.append(
            "2) 추가 문헌을 수집·triage한 뒤에만 비실행 연구 설계 proposal의 근거를 다시 평가합니다.\n"
        )
        lines.append(
            "- Evidence basis: insufficient; no empirical finding or executed experiment is reported.\n"
        )

    content = "\n".join(lines)
    write_text(out_path, content)

    legacy_output = None
    legacy_mirror_errors: list[str] = []
    if mirror_legacy:
        legacy_path = LEGACY_NOTES_ROOT / "weekly" / f"{week}.md"
        try:
            write_text(legacy_path, content)
            legacy_output = str(legacy_path)
        except OSError as exc:
            legacy_mirror_errors.append(str(exc))

    return {
        "status": "ok",
        "week": week,
        "output": str(out_path),
        "selected": len(picked),
        "legacy_output": legacy_output,
        "legacy_mirror_errors": legacy_mirror_errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--out_dir", default="notes/weekly")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--topic", default=None)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    week = iso_week_tag(datetime.now(timezone.utc))
    result = synthesize_weekly(
        triage_path=Path(args.triage),
        out_path=out_dir / f"{week}.md",
        top_n=args.top,
        mirror_legacy=False,
        domain_context=build_research_domain_context(
            run_topic=args.topic,
            source=(
                DomainContextSource.RUN_TOPIC
                if args.topic
                else DomainContextSource.LEGACY_NEUTRAL
            ),
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
