import type { ReviewResearchContext } from "../../lib/reviewPreparation";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

function readableContext(value: unknown): value is ReviewResearchContext {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const context = value as Record<string, unknown>;
  return context.schema_version === "REVIEW_CONTEXT_V1"
    && [context.topic_title, context.topic_name, context.paper_title, context.metadata_digest, context.topic_updated_at].every(item => typeof item === "string")
    && Array.isArray(context.authors) && context.authors.every(author => typeof author === "string")
    && (context.published_at === null || typeof context.published_at === "string");
}

export function SavedReviewResearchContext({ value }: { value: unknown }) {
  const { preferences } = useOptionalAppearance();
  return <section aria-label="보존된 연구 맥락" style={{ overflowWrap: "anywhere" }}>
    <h5>준비 시점에 저장한 연구 맥락</h5>
    {value === undefined || value === null ? <p>별도의 준비 맥락 기록 없음 · 이 기록에는 새 준비 맥락 항목이 없습니다. 현재 메타데이터로 채우지 않습니다.</p>
      : !readableContext(value) ? <p>보존된 연구 맥락을 해석할 수 없습니다. 현재 메타데이터로 채우지 않습니다.</p>
      : <>
        <p>저장된 주제 제목: {value.topic_title || "미기록"}</p>
        <p>연구 주제: {value.topic_name || "미기록"}</p>
        <p>논문 제목: {value.paper_title || "미기록"}</p>
        <p>저자: {value.authors.length ? value.authors.join(", ") : "미기록"}</p>
        <p>발행일: {value.published_at || "미기록"}</p>
        <p>준비 시점의 메타데이터는 연구 맥락이며, 추가로 검토한 본문이나 근거가 아닙니다. 검토 본문 범위는 보존된 선택 구간으로 확인하세요.</p>
        <details><summary>연구 맥락 보존 식별자</summary>
          <p>논문 메타데이터 식별값: {value.metadata_digest || "미기록"}</p>
          <p>저장된 주제 수정 시각: {value.topic_updated_at ? formatTimestamp(value.topic_updated_at, preferences.timeFormat, "ko-KR") : "미기록"}</p>
        </details>
      </>}
  </section>;
}
