import { type ReactNode, useEffect, useId, useState } from "react";
import { getReviewSourceCoverage, type PreparationSource, type ReviewInputOrigin, type ReviewSourceCoverageRecord, type SavedResultRanges, type SourceRange } from "../../lib/reviewPreparation";
import { V2Button } from "../V2Primitives";
import { ReviewSourcePreview } from "./ReviewSourcePreview";

export type GapPreparationControls = {
  busy: boolean; disabledReason: string; selectionLimit: number;
  onBusyChange: (busy: boolean) => void; onUseRange: (source: PreparationSource, range: SourceRange) => void | string;
};
type Props = { root: string; id: string; onOpenPreparation?: (id: string) => void; onOpenReceipt?: (id: string) => void; gapPreparation?: GapPreparationControls };
const labels: Record<ReviewInputOrigin, string> = {
  FIXTURE_SYNTHETIC: "합성 입력·결과", PROVIDER_RESPONSE: "Provider 응답 기록", MANUAL_IMPORT_UNVERIFIED: "수동 반입 기록",
};
const caveats: Record<ReviewInputOrigin, string> = {
  FIXTURE_SYNTHETIC: "앱 연결을 확인하는 합성 기록입니다. 실제 모델이 읽거나 검토한 범위가 아닙니다.",
  PROVIDER_RESPONSE: "저장된 입력·결과의 범위입니다. 실제 모델 실행이나 과학적 검토 완료를 별도로 입증하지 않습니다.",
  MANUAL_IMPORT_UNVERIFIED: "외부에서 반입한 입력·결과입니다. 실제 전달 범위와 모델 실행 여부는 검증되지 않았습니다.",
};
const ranges = (values: SourceRange[]) => values.length ? values.map(value => `${value.start}–${value.end}`).join(", ") : "없음";

function Records({ title, children }: { title: string; children: ReactNode }) {
  const [opened, setOpened] = useState(false);
  return <details onToggle={event => setOpened(event.currentTarget.open)}><summary>{title}</summary>{opened ? children : null}</details>;
}

function GapReader({ root, source, range, controls, onClose, bodyResult }: { root: string; source: PreparationSource; range: SourceRange; controls: GapPreparationControls; onClose: () => void; bodyResult?: boolean }) {
  const [invalid, setInvalid] = useState(false);
  return <section aria-label={bodyResult ? "저장된 본문 결과가 없는 구간 확인" : "미준비 구간 이어서 준비"}>
    <p>미리보기·닫기는 기존 입력을 바꾸지 않습니다. 본문을 선택하고 바꾸기 버튼을 누르면 기존 입력 출처와 범위를 교체합니다. 저장과 모델 실행은 별도입니다.</p>
    <V2Button onClick={onClose}>{bodyResult ? "본문 결과 없는 구간 닫기" : "미준비 구간 닫기"}</V2Button>
    {controls.disabledReason ? <p role="status">{controls.disabledReason}</p> : null}
    {invalid ? <p>과거 출처와 현재 파일을 연결할 수 없습니다. 기존 입력은 유지됩니다. 현재 출처를 다시 확인하세요.</p> : null}
    <ReviewSourcePreview root={root} source={source} windowRange={range} disabled={invalid || !!controls.disabledReason}
      windowLabel={bodyResult ? "선택한 결과 미보존 범위" : undefined}
      selectionLimit={controls.selectionLimit} onBusyChange={controls.onBusyChange} onSourceInvalid={() => setInvalid(true)}
      applyLabel="선택한 본문으로 입력 출처·범위 바꾸기" onUseRange={value => controls.onUseRange(source, value)}/>
  </section>;
}

export function CoverageGaps({ root, source, gaps, controls, bodyResult }: { root: string; source: PreparationSource; gaps: SourceRange[]; controls: GapPreparationControls; bodyResult?: boolean }) {
  const id = useId(), [chosen, setChosen] = useState(""), [opened, setOpened] = useState<SourceRange | null>(null);
  const key = (range: SourceRange) => `${range.start}:${range.end}`;
  const selected = gaps.find(range => key(range) === chosen) ?? gaps[0];
  if (!selected) return <p>{bodyResult ? "이 추출 텍스트에는 저장된 본문 결과가 없는 구간이 없습니다. 추출되지 않은 내용이나 전체 논문의 검토 완료를 뜻하지 않습니다." : "이 저장 출처에는 미준비 구간이 없습니다. 전체 논문 검토 완료를 뜻하지 않습니다."}</p>;
  return <div>
    <label htmlFor={id}>{bodyResult ? "살펴볼 본문 결과 없는 구간" : "살펴볼 미준비 구간"}</label>
    <select id={id} value={key(selected)} disabled={controls.busy || !!controls.disabledReason} onChange={event => setChosen(event.target.value)}>{gaps.map(range => <option key={key(range)} value={key(range)}>{range.start}–{range.end} · {range.end - range.start}자</option>)}</select>
    <V2Button disabled={controls.busy || !!controls.disabledReason} onClick={() => setOpened(selected)}>{bodyResult ? "선택한 본문 결과 없는 구간 열기" : "선택한 미준비 구간 열기"}</V2Button>
    {controls.disabledReason && !opened ? <p>{controls.disabledReason}</p> : null}
    {opened ? <GapReader key={key(opened)} root={root} source={source} range={opened} controls={controls} bodyResult={bodyResult} onClose={() => setOpened(null)}/> : null}
  </div>;
}

function Coverage({ root, id, onOpenPreparation, onOpenReceipt, gapPreparation }: Props) {
  const [opened, setOpened] = useState(false), [revision, setRevision] = useState(0);
  const [value, setValue] = useState<ReviewSourceCoverageRecord | null>(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!opened) return;
    let cancelled = false;
    setLoading(true); setValue(null); setError("");
    void getReviewSourceCoverage(root, id).then(result => {
      if (!cancelled) { setValue(result); setLoading(false); }
    }, reason => {
      if (!cancelled) {
        const message = reason instanceof Error ? reason.message : String(reason);
        setError(message.includes("review_source_coverage_limit_exceeded")
          ? "기록 수가 조회 한도를 넘어 전체 요약을 제공할 수 없습니다. 개별 준비·입력 이력에서 확인하세요."
          : message);
        setLoading(false);
      }
    });
    return () => { cancelled = true; };
  }, [root, id, opened, revision]);
  return <details className="review-preparation-record" onToggle={event => setOpened(event.currentTarget.open)}>
    <summary>보존된 출처의 범위 요약</summary>
    {opened ? <section aria-label="보존된 출처 범위">
      <p>같은 출처 관측에 연결된 이력만 합칩니다. 준비 기록과 입력·결과 기록은 별개이며, 각각 겹치는 구간을 한 번만 셉니다.</p>
      <p>전체 논문 검토 미확인 · 과학적 뒷받침 미평가</p>
      <V2Button disabled={loading} onClick={() => setRevision(previous => previous + 1)}>범위 요약 새로고침</V2Button>
      {loading ? <p role="status">범위 요약 확인 중</p> : error ? <p role="alert">범위 요약 조회 오류: {error}</p> : value ? <>
        <p>보존된 정규화 본문 {value.source.source_chars}자 기준입니다. 논문 전체 길이나 검토 완료율이 아닙니다.</p>
        <p>위치는 Unicode 코드 포인트이며 끝 위치는 포함하지 않습니다. 현재 파일 상태와 관계없는 저장 당시 범위입니다.</p>
        <p>출처 관측 SHA-256: {value.source.source_digest}</p>
        <h4>고정한 준비 구간</h4>
        <p>준비 기록 {value.prepared.record_count}건 · 중복 제외 {value.prepared.covered_chars}자</p>
        <p>준비된 범위: {ranges(value.prepared.ranges)}</p>
        <p>준비되지 않은 저장 본문 범위: {ranges(value.prepared.gaps)}</p>
        {gapPreparation ? <CoverageGaps root={root} source={value.source} gaps={value.prepared.gaps} controls={gapPreparation}/> : null}
        <Records title="준비 기록별 범위"><ul className="review-preparation-list">{value.prepared.records.map(record => <li key={record.preparation_id}>
          <p>{record.start}–{record.end} · {record.preparation_id}</p>
          {onOpenPreparation ? <V2Button aria-label={`준비 기록 열기 · ${record.preparation_id}`} onClick={() => onOpenPreparation(record.preparation_id)}>준비 기록 열기</V2Button> : null}
        </li>)}</ul></Records>
        {value.inputs.map(group => <section key={group.input_origin} aria-label={labels[group.input_origin]}>
          <h4>{labels[group.input_origin]}</h4><p>{caveats[group.input_origin]}</p>
          <p>입력·결과 기록 {group.record_count}건 · 중복 제외 {group.covered_chars}자</p>
          <p>기록된 입력 범위: {ranges(group.ranges)}</p>
          {group.records.length ? <Records title={`${labels[group.input_origin]} 기록별 범위`}><ul className="review-preparation-list">{group.records.map(record => <li key={record.receipt_id}>
            <p>{record.start}–{record.end} · {record.receipt_id}</p><p>준비 기록: {record.preparation_id}</p>
            {onOpenReceipt ? <V2Button aria-label={`입력·결과 열기 · ${record.receipt_id}`} onClick={() => onOpenReceipt(record.receipt_id)}>입력·결과 열기</V2Button> : null}
          </li>)}</ul></Records> : null}
        </section>)}
        <p>이 논문의 접근 가능한 이력 중 출처 범위에 연결되지 않은 입력 기록 {value.unmapped_receipt_count}건, 다른 출처에 연결된 입력 기록 {value.other_source_receipt_count}건은 위 합계에서 제외했습니다. 연결되지 않은 기록은 이 출처의 미검토 구간을 뜻하지 않습니다.</p>
        {value.results ? <SavedResultCoverage value={value.results} onOpenReceipt={onOpenReceipt}/> : null}
      </> : null}
    </section> : null}
  </details>;
}

export function ReviewSourceCoverage(props: Props) {
  return <Coverage key={JSON.stringify([props.root, props.id])} {...props}/>;
}

export function SavedResultCoverage({ value, onOpenReceipt }: { value: SavedResultRanges; onOpenReceipt?: (id: string) => void }) {
  const groups = [
    ["검증된 저장 반환 결과 전체", value.validated_returned], ["본문에 연결된 구조화 저장 결과", value.body_grounded],
    ["기타 검증된 저장 반환 결과", value.other_returned], ["Provider 출처만 있는 미검증 기록", value.unverified_provider],
  ] as const;
  return <section aria-label="저장된 결과 범위">
    <h4>저장된 결과와 남은 범위</h4>
    <p>보존된 입력·응답 계약 검증에 따른 분류입니다. 성공·실패·취소 등 모든 시도 이력은 포함하지 않으며, 모델이 실제로 읽었다는 별도 입증이나 문장의 과학적 타당성 평가가 아닙니다.</p>
    <p>검증된 저장 결과가 없는 범위: {ranges(value.validated_returned.gaps)}</p>
    <p>저장된 본문 결과가 없는 범위: {ranges(value.body_grounded.gaps)}</p>
    <p>위 범위는 실패한 요청이나 미실행을 뜻하지 않습니다. 해당 좌표에 연결된 검증된 저장 결과가 없다는 의미입니다. 전체와 하위 분류는 중복되므로 합산하지 않습니다.</p>
    {groups.map(([label, group]) => <section key={label} aria-label={label}><h5>{label}</h5><p>기록 {group.record_count}건 · 중복 제외 {group.covered_chars}자</p><p>결과 범위: {ranges(group.ranges)}</p>
      {group.records.length ? <Records title={`${label} 원래 입력 범위`}><ul className="review-preparation-list">{group.records.map(record => <li key={record.receipt_id}>
        <p>표시 좌표: {record.start}–{record.end} · 분류: {record.subtype}</p><p>원래 입력: {record.selection.start}–{record.selection.end} · 출처: {record.source.source_digest}</p>
        {record.mapping === "EXACT_UNIQUE_TEXT_MATCH" ? <p>원래 입력 텍스트와 유일하게 일치한 추출 구간입니다. 같은 PDF라는 입증이나 원래 입력 좌표의 변경이 아닙니다.</p> : null}
        {onOpenReceipt ? <V2Button aria-label={`결과 입력 열기 · ${record.receipt_id}`} onClick={() => onOpenReceipt(record.receipt_id)}>결과 입력 열기</V2Button> : null}
      </li>)}</ul></Records> : null}
    </section>)}
  </section>;
}
