import { FormEvent, KeyboardEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowSquareOut,
  BookmarkSimple,
  DownloadSimple,
  FileText,
  MagnifyingGlass,
  PencilSimple,
  Plus,
  Trash,
  X,
} from "@phosphor-icons/react";
import { Link } from "react-router-dom";

import type { ApiTopicDashboardResponse, ApiTopicPapersResponse } from "../../lib/api";
import {
  getTopicPaperDetail,
  type PaperBankCollectionList,
  type PaperBankDetail,
  type PaperBankExport,
  type PaperBankItem,
  type PaperBankResponse,
  type PaperBulkSaveResult,
} from "../paperBankApi";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button, V2Empty, V2Error, V2Loading, V2Panel, V2Unavailable } from "../V2Primitives";
import type { ReviewInputSelection, SynthesisSelection } from "../../lib/localSynthesis";
import { ReviewInputHistory } from "../components/ReviewInputHistory";
import { ReviewPreparation } from "../components/ReviewPreparation";
import { SavedAnalysisPanel } from "../components/SavedAnalysisPanel";
import { BrowserCollection } from "../components/BrowserCollection";

type CollectionState = "available" | "empty" | "unavailable";

export type V2PapersViewProps = {
  paperBank?: PaperBankResponse;
  collections?: PaperBankCollectionList | null;
  collectionsState?: CollectionState;
  collectionsReason?: string | null;
  papers?: ApiTopicPapersResponse;
  dashboard?: ApiTopicDashboardResponse;
  query: string;
  provider: string;
  collectionId?: string;
  collectionLabel?: string;
  collectionRouteKind?: "all" | "saved" | "named" | "malformed";
  saveTargetCollectionId?: string;
  reviewState?: string;
  year?: string;
  openAccess?: string;
  sort?: string;
  openPaperVersionId?: string;
  openEvidenceId?: string;
  onSearch: (query: string) => void;
  onProviderChange: (provider: string) => void;
  onCollectionChange?: (collectionId: string) => void;
  onSaveTargetCollectionChange?: (collectionId: string) => void;
  onReviewStateChange?: (reviewState: string) => void;
  onYearChange?: (year: string) => void;
  onOpenAccessChange?: (openAccess: string) => void;
  onSortChange?: (sort: string) => void;
  onPageChange?: (page: number) => void;
  onOpenPaper?: (paperVersionId: string) => void;
  onClosePaper?: () => void;
  onSavePaper?: (paperVersionId: string, collectionId: string) => Promise<void>;
  onRemovePaper?: (paperVersionId: string, collectionId: string) => Promise<void>;
  onBulkSave?: (paperVersionIds: string[], collectionId: string) => Promise<PaperBulkSaveResult>;
  onExport?: (query: { q?: string; provider?: string; collectionId?: string; year?: string; reviewState?: string; openAccess?: string; sort?: string; page?: number; pageSize?: number }) => Promise<PaperBankExport>;
  onCreateCollection?: (name: string) => Promise<void>;
  onRenameCollection?: (collectionId: string, name: string) => Promise<void>;
  onDeleteCollection?: (collectionId: string) => Promise<void>;
  onRetryCollections?: () => void;
  onFreezeSelection?: (selection:SynthesisSelection[], requestId:string) => Promise<void>;
  onCollectionImported?: () => Promise<void>;
};

function formatDate(value?: string | null) {
  if (!value) return "미확인";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" });
}

function legacyPaperBank(papers: ApiTopicPapersResponse): PaperBankResponse {
  return {
    topicId: papers.topic?.id ?? "legacy-topic",
    items: papers.items.map((paper) => ({
      canonicalPaperId: `legacy-${paper.id}`,
      paperVersionId: `legacy-${paper.runId}-${paper.id}`,
      sourceIdentity: { provider: paper.sourceProvider ?? "unknown", nativeId: paper.id, doi: null },
      compatibility: {
        runId: paper.runId,
        paperId: paper.id,
        occurrences: [{ runId: paper.runId, paperId: paper.id }],
      },
      metadataDigest: "legacy-compatibility",
      title: paper.title,
      authors: paper.authors,
      publishedAt: paper.publishedAt ?? null,
      sourceProvider: paper.sourceProvider ?? "unknown",
      sourceUrl: paper.sourceUrl ?? null,
      sourceUrlState: paper.sourceUrl ? "available" : "unavailable",
      theme: paper.theme ?? null,
      review: { state: "not_reviewed", updatedAt: null, evidenceCount: 0 },
      collectionIds: paper.saved ? ["saved-papers"] : [],
    })),
    total: papers.total,
    returnedCount: papers.items.length,
    page: 1,
    pageSize: Math.max(1, papers.items.length),
    totalPages: papers.items.length ? 1 : 0,
    canonicalQuery: {
      topicId: papers.topic?.id ?? "legacy-topic",
      q: papers.q,
      provider: papers.providerFilter ?? null,
      collectionId: null,
      reviewState: null,
      sort: "published_desc",
    },
    queryDigest: "legacy-compatibility",
    providerCounts: papers.providerCounts,
    reviewResource: { state: "unavailable", reason: "M2 review projection not supplied" },
    reviewSummary: null,
    collectionResource: { state: "unavailable", reason: "M2 collection projection not supplied" },
    resource: { state: papers.items.length ? "available" : "empty", reason: null },
    unsupportedFilters: [
      { id: "citation", state: "unavailable", reason: "관측된 인용 계약이 없습니다." },
      { id: "impact", state: "unavailable", reason: "영향력 계약은 M3 범위입니다." },
    ],
  };
}

function reviewLabel(state: PaperBankItem["review"]["state"]) {
  if (state === "full_text_reviewed") return "본문 처리 기록";
  if (state === "full_text_available") return "전문 사용 가능";
  if (state === "abstract_reviewed") return "초록 검토";
  if (state === "unavailable") return "검토 저장소 사용 불가";
  return "미검토";
}

function unavailableReason(bank: PaperBankResponse, id: string, fallback: string) {
  return bank.unsupportedFilters.find((item) => item.id === id)?.reason ?? fallback;
}

function locatorText(locator: Record<string, unknown> | null) {
  return locator ? JSON.stringify(locator) : "locator 없음";
}

function evidenceDestination(
  destination: string,
  context: Pick<V2PapersViewProps, "collectionId" | "reviewState" | "year" | "openAccess" | "sort"> & { page?: number },
) {
  const target = new URL(destination, window.location.origin);
  const collection = context.collectionId === "saved-papers" ? "saved" : context.collectionId;
  if (collection) target.searchParams.set("collection", collection);
  if (context.reviewState) target.searchParams.set("review", context.reviewState);
  if (context.year) target.searchParams.set("year", context.year);
  if (context.openAccess) target.searchParams.set("openAccess", context.openAccess);
  if (context.sort && context.sort !== "published_desc") target.searchParams.set("sort", context.sort);
  if (context.page && context.page > 1) target.searchParams.set("page", String(context.page));
  return `${target.pathname}${target.search}${target.hash}`;
}

export function V2PapersView(props: V2PapersViewProps) {
  const { preferences } = useOptionalAppearance();
  const bank = useMemo(
    () => props.paperBank ?? (props.papers ? legacyPaperBank(props.papers) : null),
    [props.paperBank, props.papers],
  );
  const legacyMode = !props.paperBank;
  const [draftQuery, setDraftQuery] = useState(props.query);
  const [selected, setSelected] = useState<string[]>([]);
  const [selectedReviews, setSelectedReviews] = useState<Record<string, { title: string; selection: ReviewInputSelection }>>({});
  const [openedReviewReceipt, setOpenedReviewReceipt] = useState<{ topicId: string; paperVersionId: string; request: { receiptId: string; revision: number } } | null>(null);
  const selectedDigests=useRef<Record<string,string>>({});
  const freezeAttempt=useRef<{fingerprint:string;requestId:string}|null>(null);
  const [uncontrolledSaveTargetCollectionId, setUncontrolledSaveTargetCollectionId] = useState("saved-papers");
  const [newCollectionName, setNewCollectionName] = useState("");
  const [renameValues, setRenameValues] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [loadedDetail, setDetail] = useState<PaperBankDetail | null>(null);
  const detail = loadedDetail?.topicId === bank?.topicId && loadedDetail?.paperVersionId === props.openPaperVersionId ? loadedDetail : null;
  const [detailPhase, setDetailPhase] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRetryKey, setDetailRetryKey] = useState(0);
  const [exportResult, setExportResult] = useState<PaperBankExport | null>(null);
  const bulkResultRef = useRef<PaperBulkSaveResult | null>(null);
  const detailRegionRef = useRef<HTMLElement | null>(null);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);
  const saveTargetCollectionId = props.saveTargetCollectionId ?? uncontrolledSaveTargetCollectionId;

  function updateSaveTargetCollectionId(collectionId: string) {
    setNotice(null);
    if (props.onSaveTargetCollectionChange) {
      props.onSaveTargetCollectionChange(collectionId);
      return;
    }
    setUncontrolledSaveTargetCollectionId(collectionId);
  }

  useEffect(() => setDraftQuery(props.query), [props.query]);
  useEffect(() => {setSelected([]);setSelectedReviews({});selectedDigests.current={};freezeAttempt.current=null;}, [bank?.topicId]);
  useEffect(() => {
    if (!props.collections || props.collections.items.some((item) => item.id === saveTargetCollectionId)) return;
    const fallback = props.collections.items.find((item) => item.id === "saved-papers");
    if (!fallback) return;
    updateSaveTargetCollectionId(fallback.id);
    setNotice(`선택한 저장 대상 컬렉션을 사용할 수 없어 ${fallback.name}로 변경했습니다.`);
  }, [props.collections, saveTargetCollectionId]);

  useEffect(() => {
    const paperVersionId = props.openPaperVersionId ?? "";
    if (!paperVersionId || !bank || legacyMode) {
      setDetail(null);
      setDetailPhase("idle");
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailPhase("loading");
    setDetailError(null);
    void getTopicPaperDetail(bank.topicId, paperVersionId)
      .then((payload) => {
        if (cancelled) return;
        setDetail(payload);
        setDetailPhase("ready");
        window.setTimeout(() => detailRegionRef.current?.focus(), 0);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setDetail(null);
        setDetailError(error instanceof Error ? error.message : "논문 상세를 불러오지 못했습니다.");
        setDetailPhase("error");
      });
    return () => {
      cancelled = true;
    };
  }, [bank, detailRetryKey, legacyMode, props.openPaperVersionId]);

  useEffect(() => {
    if (!props.openEvidenceId || detailPhase !== "ready") return;
    const target = Array.from(document.querySelectorAll<HTMLElement>("[data-evidence-id]"))
      .find((candidate) => candidate.dataset.evidenceId === props.openEvidenceId);
    target?.scrollIntoView({ block: "center" });
    target?.focus({ preventScroll: true });
  }, [detailPhase, props.openEvidenceId]);

  if (!bank) {
    return <V2Unavailable title="논문 목록을 사용할 수 없습니다." reason="paper_bank_response_missing" />;
  }

  const allSelected = bank.items.length > 0 && bank.items.every((paper) => selected.includes(paper.paperVersionId));
  const providerOptions = bank.providerCounts.map((item) => ({ value: item.provider, label: item.label }));
  const facets = bank.facets;
  const yearOptions = facets?.year.options ?? [];
  const reviewOptions = facets?.reviewLevel.options ?? [
    { value: "not_reviewed", label: "미검토", count: 0 },
    { value: "abstract_reviewed", label: "초록 검토", count: 0 },
    { value: "full_text_available", label: "전문 사용 가능", count: 0 },
    { value: "full_text_reviewed", label: "본문 처리 기록", count: 0 },
  ];
  const accessOptions = facets?.access.options ?? [];
  const collectionsState = props.collectionsState ?? (props.collections ? "available" : legacyMode ? "empty" : "unavailable");
  const activeCollectionId = saveTargetCollectionId;
  const citationReason = unavailableReason(bank, "citation", "관측된 인용 계약이 없습니다.");
  const impactReason = unavailableReason(bank, "impact", "영향력 계약은 M3 범위입니다.");
  const collectionEmptyTitle = props.collectionRouteKind === "saved"
    ? "Saved papers 컬렉션은 존재하며 저장된 논문이 0개입니다."
    : props.collectionRouteKind === "named"
      ? `${props.collectionLabel ?? props.collectionId} 컬렉션은 존재하며 저장된 논문이 0개입니다.`
      : null;

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    props.onSearch(draftQuery.trim());
  }

  function togglePaper(id: string) {
    const paper=bank!.items.find(item=>item.paperVersionId===id);
    if(paper)selectedDigests.current[id]=paper.metadataDigest;
    setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
    if(selected.includes(id))setSelectedReviews(current=>Object.fromEntries(Object.entries(current).filter(([key])=>key!==id)));
  }

  function selectPage() {
    for(const paper of bank!.items)if(!selected.includes(paper.paperVersionId))selectedDigests.current[paper.paperVersionId]=paper.metadataDigest;
    const pageIds=bank!.items.map(paper=>paper.paperVersionId);
    setSelected(current=>allSelected?current.filter(id=>!pageIds.includes(id)):[...new Set([...current,...pageIds])]);
    if(allSelected)setSelectedReviews(current=>Object.fromEntries(Object.entries(current).filter(([key])=>!pageIds.includes(key))));
  }

  function chooseReviewInput(paperVersionId: string, selection: ReviewInputSelection) {
    const paper = bank!.items.find(item=>item.paperVersionId===paperVersionId) ?? (detail?.paperVersionId===paperVersionId ? detail : null);
    if(!paper || busyKey!==null || !props.onFreezeSelection)return;
    if(!selected.includes(paperVersionId))selectedDigests.current[paperVersionId]=paper.metadataDigest;
    setSelected(current=>current.includes(paperVersionId)?current:[...current,paperVersionId]);
    setSelectedReviews(current=>({...current,[paperVersionId]:{title:paper.title,selection}}));
    setNotice("검토 기록과 논문을 보고서 입력으로 선택했습니다. 입력 고정 전에는 이 논문 화면에서만 유지됩니다.");
  }

  async function freezeSelection() {
    if(!props.onFreezeSelection||busyKey!==null||!selected.length)return;
    const selection=selected.slice().sort().map(id=>({paper_version_id:id,metadata_digest:selectedDigests.current[id],...(selectedReviews[id]?{review_input:selectedReviews[id].selection}:{})}));
    const fingerprint=JSON.stringify(selection);
    if(freezeAttempt.current?.fingerprint!==fingerprint)freezeAttempt.current={fingerprint,requestId:crypto.randomUUID()};
    setBusyKey("freeze");setMutationError(null);
    try{await props.onFreezeSelection(selection,freezeAttempt.current.requestId);}
    catch(error){setMutationError(error instanceof Error?error.message:"선택 입력을 고정하지 못했습니다.");}
    finally{setBusyKey(null);}
  }

  function openDetail(event: MouseEvent<HTMLButtonElement>, paperVersionId: string) {
    detailTriggerRef.current = event.currentTarget;
    props.onOpenPaper?.(paperVersionId);
  }

  function closeDetail() {
    props.onClosePaper?.();
    setDetail(null);
    setDetailPhase("idle");
    window.setTimeout(() => detailTriggerRef.current?.focus(), 0);
  }

  function handleDetailKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDetail();
    }
  }

  async function mutate(key: string, action: () => Promise<void>, success: string) {
    setBusyKey(key);
    setMutationError(null);
    setNotice(null);
    try {
      await action();
      setNotice(success);
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : "변경을 저장하지 못했습니다.");
    } finally {
      setBusyKey(null);
    }
  }

  async function toggleSavedPaper(paper: PaperBankItem) {
    const saved = paper.collectionIds.includes(activeCollectionId);
    if (saved && !props.onRemovePaper) return;
    if (!saved && !props.onSavePaper) return;
    await mutate(
      `${saved ? "remove" : "save"}:${paper.paperVersionId}`,
      () => saved
        ? props.onRemovePaper!(paper.paperVersionId, activeCollectionId)
        : props.onSavePaper!(paper.paperVersionId, activeCollectionId),
      saved
        ? `${paper.title}을(를) 선택한 컬렉션에서 제거했습니다.`
        : `${paper.title}을(를) ${activeCollectionId === "saved-papers" ? "Saved papers에" : "선택한 컬렉션에"} 저장했습니다.`,
    );
  }

  async function createCollection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newCollectionName.trim();
    if (!name || !props.onCreateCollection) return;
    await mutate("collection:create", () => props.onCreateCollection!(name), `${name} 컬렉션을 생성했습니다.`);
    setNewCollectionName("");
  }

  async function bulkSaveSelected() {
    if (!selected.length || !props.onBulkSave) return;
    bulkResultRef.current = null;
    await mutate(
      "bulk-save",
      async () => {
        bulkResultRef.current = await props.onBulkSave!(selected, activeCollectionId);
        setSelected([]);
      },
      "",
    );
    const result = bulkResultRef.current as PaperBulkSaveResult | null;
    if (result) {
      setNotice(`${result.addedCount}개를 저장했고 ${result.alreadyPresentCount}개는 이미 저장되어 있습니다. 총 ${result.finalMemberCount}개입니다.`);
    }
  }

  async function exportAllRows() {
    if (!props.onExport) return;
    setExportResult(null);
    await mutate(
      "export",
      async () => {
        const result = await props.onExport!({
          q: props.query || undefined,
          provider: props.provider || undefined,
          collectionId: props.collectionId || undefined,
          year: props.year || undefined,
          reviewState: props.reviewState || undefined,
          openAccess: props.openAccess || undefined,
          sort: props.sort,
          page: bank!.page,
          pageSize: bank!.pageSize,
        });
        setExportResult(result);
      },
      "CSV 내보내기를 준비했습니다.",
    );
  }

  return (
    <div className="v2-papers-layout" data-resource-state={bank.resource.state}>
      <section className="v2-papers-main">
        {!legacyMode ? <BrowserCollection topicId={bank.topicId} onSaved={props.onCollectionImported} onOpenPaper={props.onOpenPaper}/> : null}
        {!legacyMode ? <SavedAnalysisPanel topicId={bank.topicId}/> : null}
        <div className="v2-paper-filterbar">
          <form role="search" onSubmit={submitSearch}>
            <MagnifyingGlass size={16} />
            <input aria-label="논문 제목, 저자, 키워드 검색" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="논문 제목, 저자, 키워드 검색..." />
          </form>
          <select aria-label="연도 필터" value={props.year ?? ""} disabled={!props.onYearChange || (facets?.year.state === "unavailable")} onChange={(event) => props.onYearChange?.(event.target.value)}><option value="">전체 연도</option>{yearOptions.map((item) => <option key={item.value} value={item.value}>{item.label} ({item.count})</option>)}</select>
          <select aria-label="출처" value={props.provider} onChange={(event) => props.onProviderChange(event.target.value)}><option value="">전체 출처</option>{providerOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
          <select aria-label="표시할 컬렉션" value={props.collectionId ?? ""} disabled={collectionsState === "unavailable"} onChange={(event) => props.onCollectionChange?.(event.target.value)}><option value="">전체 컬렉션</option>{props.collections?.items.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.memberCount})</option>)}</select>
          <select aria-label="리뷰 상태" value={props.reviewState ?? ""} disabled={bank.reviewResource.state === "unavailable"} onChange={(event) => props.onReviewStateChange?.(event.target.value)}><option value="">전체 리뷰 상태</option>{reviewOptions.map((item) => <option key={item.value} value={item.value}>{item.value === "full_text_reviewed" ? "본문 처리 기록" : item.label} ({item.count})</option>)}</select>
          <select aria-label="접근 권리" value={props.openAccess ?? ""} disabled={!props.onOpenAccessChange || (facets?.access.state === "unavailable")} onChange={(event) => props.onOpenAccessChange?.(event.target.value)}><option value="">전체 접근 권리</option>{accessOptions.map((item) => <option key={item.value} value={item.value}>{item.label} ({item.count})</option>)}</select>
          <select aria-label="인용수 필터" disabled title={citationReason}><option>인용수 · 사용 불가</option></select>
          <select aria-label="영향력 필터" disabled title={impactReason}><option>영향력 · 사용 불가</option></select>
          <select aria-label="정렬" value={props.sort ?? "published_desc"} onChange={(event) => props.onSortChange?.(event.target.value)}><option value="published_desc">발표일순</option><option value="title_asc">제목순</option><option value="provider_asc">출처순</option><option value="review_state">리뷰 상태순</option></select>
        </div>

        <div className="v2-selection-toolbar">
          <span>{selected.length} 선택됨</span>
          {Object.keys(selectedReviews).length ? <span>검토 기록 {Object.keys(selectedReviews).length}개 선택</span> : null}
          <V2Button disabled={!selected.length || !props.onBulkSave || busyKey !== null} busy={busyKey === "bulk-save"} onClick={() => void bulkSaveSelected()} title={!props.onBulkSave ? "벌크 저장 API가 연결되지 않았습니다." : "선택한 논문을 한 번에 저장"}><BookmarkSimple size={15} />선택 저장</V2Button>
          <V2Button disabled={!props.onExport || busyKey !== null} busy={busyKey === "export"} onClick={() => void exportAllRows()} title={!props.onExport ? "CSV export API가 연결되지 않았습니다." : "현재 필터의 모든 논문을 CSV로 내보내기"}><DownloadSimple size={15} />CSV 내보내기</V2Button>
          <V2Button disabled={legacyMode||!props.onFreezeSelection||!selected.length||selected.length>100||busyKey!==null} busy={busyKey==="freeze"} onClick={()=>void freezeSelection()} title={legacyMode||!props.onFreezeSelection?"이 화면에는 고정 입력 API가 연결되지 않았습니다.":selected.length>100?"한 초안에는 최대 100편을 선택할 수 있습니다.":"선택한 최대 100편의 현재 초록·저장 근거를 고정해 수동 근거 초안을 준비합니다."}><FileText size={15} />선택 보고서 입력 고정</V2Button>
          {exportResult ? <a className="v2-text-link" href={exportResult.downloadUrl} download={exportResult.fileName}>다운로드 ({exportResult.rowCount}개)</a> : null}
          <strong>총 {bank.total.toLocaleString()}개 논문</strong>
        </div>
        {Object.keys(selectedReviews).length ? <section className="review-input-selection" aria-label="보고서에 선택한 검토 기록"><p>입력 고정 전의 선택입니다. 조회한 다른 기록으로 자동 교체하지 않습니다.</p><ul>{Object.entries(selectedReviews).map(([versionId, item])=><li key={versionId}><span>{item.title} · {item.selection.component_ids.length}개 구간 · {item.selection.receipt_id}</span> <V2Button disabled={busyKey!==null} aria-label={`검토 기록 선택 해제 · ${item.selection.receipt_id}`} onClick={()=>setSelectedReviews(current=>Object.fromEntries(Object.entries(current).filter(([key])=>key!==versionId)))}>검토 기록 선택 해제</V2Button></li>)}</ul></section> : null}
        {legacyMode && selected.length ? <p className="v2-inline-notice is-warning" role="status">선택 상태는 현재 화면에서만 유지됩니다. 저장·보고서 corpus 작업은 아직 실행되지 않습니다.</p> : null}
        {notice ? <p className="v2-inline-notice" role="status">{notice}</p> : null}
        {mutationError ? <V2Error title="논문 은행 변경을 저장하지 못했습니다." message={mutationError} onDismiss={() => setMutationError(null)} /> : null}

        <div className="v2-paper-table-panel">
          <table className="v2-paper-table" aria-label="논문 목록">
            <thead><tr><th scope="col"><label><input type="checkbox" checked={allSelected} disabled={busyKey==="freeze"} onChange={selectPage} /><span className="sr-only">전체 선택</span></label></th><th scope="col">#</th><th scope="col">논문 정보</th><th scope="col">발표일</th><th scope="col">리뷰</th><th scope="col">증거</th><th scope="col">작업</th></tr></thead>
            <tbody>
              {bank.items.map((paper, index) => (
                <tr key={paper.paperVersionId}>
                  <td><label><input type="checkbox" checked={selected.includes(paper.paperVersionId)} disabled={busyKey==="freeze"} onChange={() => togglePaper(paper.paperVersionId)} /><span className="sr-only">{paper.title} 선택</span></label></td>
                  <td>{(bank.page - 1) * bank.pageSize + index + 1}</td>
                  <td className="v2-paper-row-info"><button type="button" className="v2-paper-title-button" aria-label={`${paper.title} 상세 열기`} onClick={(event) => openDetail(event, paper.paperVersionId)}>{paper.title}</button><small>{paper.authors.join(", ") || "저자 정보 없음"}</small><p>{paper.sourceIdentity.nativeId ?? paper.compatibility.paperId}</p><div><i>{paper.sourceProvider}</i>{paper.theme ? <i>{paper.theme}</i> : null}<i className={`v2-paper-access is-${paper.accessState ?? "unknown"}`}>{paper.accessLabel ?? (paper.accessState === "open_access" ? "Open access" : "권리 미확인")}</i></div></td>
                  <td>{paper.publicationYearState === "known" && paper.publicationYear ? `${paper.publicationYear} · ` : ""}{formatDate(paper.publishedAt)}</td>
                  <td><span className={`v2-paper-state is-${paper.review.state}`}>{reviewLabel(paper.review.state)}</span></td>
                  <td>{paper.review.state === "unavailable" ? "사용 불가" : paper.review.evidenceCount}</td>
                  <td><div className="v2-row-actions"><button type="button" aria-label={`${paper.title} ${paper.collectionIds.includes(activeCollectionId) ? "저장 해제" : "저장"}`} disabled={paper.collectionIds.includes(activeCollectionId) ? !props.onRemovePaper || busyKey !== null : !props.onSavePaper || busyKey !== null} onClick={() => void toggleSavedPaper(paper)} title={paper.collectionIds.includes(activeCollectionId) ? (props.onRemovePaper ? "선택한 컬렉션에서 제거" : "M2 persistence client가 연결되지 않았습니다.") : (props.onSavePaper ? "선택한 컬렉션에 저장" : "M2 persistence client가 연결되지 않았습니다.")}><BookmarkSimple size={16} weight={paper.collectionIds.includes(activeCollectionId) ? "fill" : "regular"} /></button>{paper.sourceUrlState === "available" && paper.sourceUrl ? <a href={paper.sourceUrl} target="_blank" rel="noreferrer" aria-label={`${paper.title} 원문 열기`}><ArrowSquareOut size={16} /></a> : <span className="v2-unavailable-glyph" aria-label="검증된 원문 URL 없음">—</span>}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!bank.items.length ? <V2Empty title={collectionEmptyTitle ?? (bank.resource.state === "empty" ? "이 주제에는 아직 논문이 없습니다." : "검색 조건에 맞는 논문이 없습니다.")} description={collectionEmptyTitle ? "컬렉션은 정상적으로 조회되었으며 저장된 membership이 없습니다." : bank.resource.state === "empty" ? "실제 수집 run이 완료되면 이곳에 표시됩니다." : "검색어 또는 지원되는 필터를 조정해 보세요."} /> : null}
          <nav className="v2-pagination" aria-label="논문 페이지 탐색"><V2Button aria-label="이전 페이지" disabled={bank.page <= 1} onClick={() => props.onPageChange?.(bank.page - 1)}><ArrowLeft size={14} />이전</V2Button><span aria-current="page">{bank.totalPages ? `${bank.page} / ${bank.totalPages}` : "0 / 0"} · 응답 {bank.returnedCount}개 · query {bank.queryDigest.slice(0, 10)}</span><V2Button aria-label="다음 페이지" disabled={bank.totalPages === 0 || bank.page >= bank.totalPages} onClick={() => props.onPageChange?.(bank.page + 1)}>다음<ArrowRight size={14} /></V2Button></nav>
        </div>
      </section>

      <aside className="v2-paper-rail">
        <V2Panel title="필터 요약" action={<button type="button" className="v2-text-link" onClick={() => { setDraftQuery(""); props.onSearch(""); props.onProviderChange(""); props.onCollectionChange?.(""); props.onReviewStateChange?.(""); props.onYearChange?.(""); props.onOpenAccessChange?.(""); }}>모두 초기화</button>}><dl className="v2-filter-summary"><div><dt>검색어</dt><dd>{props.query || "없음"}</dd></div><div><dt>출처</dt><dd>{props.provider || "전체"}</dd></div><div><dt>컬렉션</dt><dd>{props.collectionLabel ?? props.collectionId ?? "전체"}</dd></div><div><dt>연도</dt><dd>{props.year || "전체"}</dd></div><div><dt>리뷰</dt><dd>{props.reviewState === "full_text_reviewed" ? "본문 처리 기록" : props.reviewState || "전체"}</dd></div><div><dt>접근 권리</dt><dd>{props.openAccess || "전체"}</dd></div></dl></V2Panel>

        <V2Panel title="저장한 컬렉션">
          <div className="v2-collection-management" role="region" aria-label="컬렉션 관리">
            {collectionsState === "unavailable" ? <V2Unavailable title="컬렉션을 사용할 수 없습니다." reason={props.collectionsReason ?? "paper_collection_store_unavailable"} onRetry={props.onRetryCollections} /> : null}
            {collectionsState !== "unavailable" || props.collections?.items.length ? <><label className="v2-collection-target"><span>행 저장 대상</span><select aria-label="저장 대상 컬렉션" value={saveTargetCollectionId} disabled={collectionsState === "unavailable"} onChange={(event) => updateSaveTargetCollectionId(event.target.value)}>{props.collections?.items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><form className="v2-collection-create" onSubmit={(event) => void createCollection(event)}><input aria-label="새 컬렉션 이름" value={newCollectionName} disabled={collectionsState === "unavailable"} onChange={(event) => setNewCollectionName(event.target.value)} placeholder="새 컬렉션" maxLength={80} /><V2Button aria-label="컬렉션 생성" disabled={collectionsState === "unavailable" || !props.onCreateCollection || !newCollectionName.trim() || busyKey !== null}><Plus size={14} />생성</V2Button></form><ul className="v2-collection-list">{props.collections?.items.map((collection) => <li key={collection.id}><div><strong>{collection.name}</strong><span>{collection.memberCount}개</span></div>{!collection.isDefault ? <div className="v2-collection-actions"><input aria-label={`${collection.name} 새 이름`} value={renameValues[collection.id] ?? collection.name} disabled={collectionsState === "unavailable"} onChange={(event) => setRenameValues((current) => ({ ...current, [collection.id]: event.target.value }))} /><button type="button" aria-label={`${collection.name} 이름 변경`} disabled={collectionsState === "unavailable" || !props.onRenameCollection || busyKey !== null} onClick={() => void mutate(`rename:${collection.id}`, () => props.onRenameCollection!(collection.id, (renameValues[collection.id] ?? collection.name).trim()), "컬렉션 이름을 변경했습니다.")}><PencilSimple size={14} /></button><button type="button" aria-label={`${collection.name} 삭제`} disabled={collectionsState === "unavailable" || !props.onDeleteCollection || busyKey !== null} onClick={() => void mutate(`delete:${collection.id}`, () => props.onDeleteCollection!(collection.id), "컬렉션을 삭제했습니다.")}><Trash size={14} /></button></div> : null}</li>)}</ul>{!props.collections?.items.length ? <V2Empty title="컬렉션이 없습니다." description="첫 named collection을 생성하세요." /> : null}</> : null}
          </div>
        </V2Panel>

        <V2Panel title="읽기 상태">{bank.reviewResource.state === "unavailable" || !bank.reviewSummary ? <V2Unavailable title="리뷰 상태를 사용할 수 없습니다." reason={bank.reviewResource.reason ?? "paper_review_store_unavailable"} /> : <dl className="v2-review-summary"><div><dt>미검토</dt><dd>{bank.reviewSummary.notReviewed}</dd></div><div><dt>초록 검토</dt><dd>{bank.reviewSummary.abstractReviewed}</dd></div><div><dt>전문 사용 가능</dt><dd>{bank.reviewSummary.fullTextAvailable}</dd></div><div><dt>본문 처리 기록</dt><dd>{bank.reviewSummary.fullTextReviewed}</dd></div></dl>}<p className="v2-tip-copy">본문 처리 기록에는 규칙 기반·일부 본문 처리가 포함될 수 있습니다. AI가 논문 전체를 검토했다는 뜻은 아닙니다.</p></V2Panel>
        <V2Panel title="검색 범위"><p className="v2-tip-copy">검색·출처·컬렉션·연도·접근 권리·리뷰·정렬·페이지는 서버 query contract를 사용합니다. 인용·영향력은 활성화하지 않습니다.</p><Link className="v2-text-link" to="/evidence?view=search-help">증거 검색 도움말</Link></V2Panel>
      </aside>

      {props.openPaperVersionId ? <section className="v2-paper-detail-drawer" role="region" aria-label="논문 상세" tabIndex={-1} ref={detailRegionRef} onKeyDown={handleDetailKeyDown}><header><div><span>Paper version detail</span><h2>{detail?.title ?? "논문 상세"}</h2></div><button type="button" aria-label="논문 상세 닫기" onClick={closeDetail}><X size={18} /></button></header>{detailPhase === "loading" ? <V2Loading label="논문 상세를 불러오는 중입니다." /> : null}{detailPhase === "error" ? <V2Error title="논문 상세를 불러오지 못했습니다." message={detailError ?? "논문 상세를 불러오지 못했습니다."} onRetry={() => setDetailRetryKey((current) => current + 1)} /> : null}{detail ? <div className="v2-paper-detail-body"><dl><div><dt>Canonical paper</dt><dd>{detail.canonicalPaperId}</dd></div><div><dt>Paper version</dt><dd>{detail.paperVersionId}</dd></div><div><dt>Provider identity</dt><dd>{detail.sourceIdentity.provider}:{detail.sourceIdentity.nativeId ?? "unknown"}</dd></div><div><dt>DOI</dt><dd>{detail.sourceIdentity.doi ?? "미확인"}</dd></div><div><dt>Review</dt><dd>{reviewLabel(detail.review.state)}</dd></div><div><dt>Review updated</dt><dd>{detail.review.updatedAt ? formatTimestamp(detail.review.updatedAt, preferences.timeFormat ?? "locale", "ko-KR") : "미확인"}</dd></div><div><dt>기존 리뷰의 본문 상태</dt><dd>{detail.fullText.state}</dd></div><div><dt>Rights / OA</dt><dd>{detail.rights.state}{detail.rights.label ? ` · ${detail.rights.label}` : ""}</dd></div><div><dt>Metadata digest</dt><dd>{detail.metadataDigest}</dd></div></dl><article><h3>저장된 초록</h3><p>{detail.abstract ?? "저장된 초록이 없습니다."}</p></article><section><h3>Evidence ({detail.evidence.length})</h3>{detail.evidenceResource.state === "unavailable" ? <V2Unavailable title="Evidence를 사용할 수 없습니다." reason={detail.evidenceResource.reason ?? "paper_evidence_unavailable"} onRetry={() => setDetailRetryKey((current) => current + 1)} /> : detail.evidence.length ? <ul className="v2-evidence-list">{detail.evidence.map((evidence) => <li key={evidence.id} data-evidence-id={evidence.id} tabIndex={-1} className={props.openEvidenceId === evidence.id ? "is-evidence-target" : undefined}><strong>{evidence.id}</strong><span>{locatorText(evidence.locator)}</span><code>{evidence.contentDigest ?? "digest 없음"}</code><p>{evidence.text ?? "표시 가능한 evidence text 없음"}</p>{evidence.destinationState === "available" && evidence.destination ? <Link to={evidenceDestination(evidence.destination, { ...props, page: bank.page })}>증거 provenance 열기</Link> : <span>증거 목적지 사용 불가</span>}</li>)}</ul> : <V2Empty title="연결된 evidence가 없습니다." description="없음과 evidence 서비스 오류는 별도로 표시됩니다." />}</section></div> : null}{!legacyMode ? <ReviewPreparation topicId={bank.topicId} paperVersionId={props.openPaperVersionId} onOpenReceipt={receiptId => setOpenedReviewReceipt(previous => ({ topicId: bank.topicId, paperVersionId: props.openPaperVersionId!, request: { receiptId, revision: (previous?.request.revision ?? 0) + 1 } }))}/> : null}{!legacyMode ? <ReviewInputHistory topicId={bank.topicId} paperVersionId={props.openPaperVersionId} openReceipt={openedReviewReceipt?.topicId === bank.topicId && openedReviewReceipt.paperVersionId === props.openPaperVersionId ? openedReviewReceipt.request : undefined} selectedReportReceiptId={selectedReviews[props.openPaperVersionId]?.selection.receipt_id} onSelectForReport={props.onFreezeSelection ? selection=>chooseReviewInput(props.openPaperVersionId!,selection) : undefined} reportSelectionDisabledReason={busyKey!==null ? "다른 저장 작업이 끝난 뒤 선택하세요." : !(bank.items.some(item=>item.paperVersionId===props.openPaperVersionId)||detail?.paperVersionId===props.openPaperVersionId) ? "이 논문의 현재 메타데이터를 확인한 뒤 선택하세요." : undefined}/> : null}</section> : null}
    </div>
  );
}
