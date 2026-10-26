import { fetchJson } from "./api";
import type { AnalysisUnit } from "./reviewDocumentPlan";

export type PreparationSource = {
  run_id: string; paper_id: string; source_digest: string; source_chars: number; source_kind: string;
  artifact_sha256: string | null; artifact_bytes: number | null; extraction_truncation: string;
  extraction_id?: string;
};
export type UnavailablePreparationSource = { diagnostic_id: string; run_id: string; reason: string; extraction_id?: string };
export type PreparationSources = { sources: PreparationSource[]; unavailable_sources?: UnavailablePreparationSource[]; limits: { max_selected_chars: number }; state: "NOT_EXECUTED"; reason?: string };
export type PreparationRequest = { request_id: string; run_id: string; paper_id: string; expected_source_digest: string; start: number; end: number };
export type PreparationPreviewRequest = { run_id: string; paper_id: string; expected_source_digest: string; start: number; length: number };
export type PreparationPreview = {
  schema_version: "PAPER_REVIEW_PREVIEW_V1"; source: PreparationSource;
  window: { start: number; end: number; text: string; chars: number };
  normalization: "XRML_WHITESPACE_V1"; decoding: "UTF8_STRICT" | "UTF8_SIG_STRICT"; state: "NOT_EXECUTED";
};
export type ReviewResearchContext = {
  schema_version: "REVIEW_CONTEXT_V1"; topic_title: string; topic_name: string; paper_title: string;
  authors: string[]; published_at: string | null; metadata_digest: string; topic_updated_at: string;
};
type PreparationVersion = { schema_version: "PAPER_REVIEW_PREPARATION_V1"; context?: never }
  | { schema_version: "PAPER_REVIEW_PREPARATION_V2"; context: ReviewResearchContext }
  | { schema_version: "PAPER_REVIEW_PREPARATION_V3"; context: ReviewResearchContext; analysis_unit: AnalysisUnit };
type ReviewPreparationBody = {
  preparation_id: string; topic_id: string; paper_version_id: string;
  actor_id: string; created_at: string; state: "PREPARED_NOT_EXECUTED";
  source: PreparationSource & { artifact_ref: string | null };
  selection: { start: number; end: number; text: string; chars: number; sha256: string };
  execution: { started: false; full_document_review: "NOT_ESTABLISHED" };
};
export type ReviewPreparationRecord = ReviewPreparationBody & PreparationVersion;
export type PreparationSummary = Omit<ReviewPreparationBody, "selection"> & PreparationVersion & { selection: Omit<ReviewPreparationRecord["selection"], "text"> };
export type PreparationList = { items: PreparationSummary[]; hasMore: boolean; page: number; pageSize: number };
export type ReviewPreparationBinding = {
  preparation_id: string; preparation_sha256: string;
  topic_id: string; paper_version_id: string; source: ReviewPreparationRecord["source"];
  selection: Omit<ReviewPreparationRecord["selection"], "text">;
  normalization: "XRML_WHITESPACE_V1"; decoding: "UTF8_STRICT" | "UTF8_SIG_STRICT";
} & ({ schema_version: "REVIEW_PREPARATION_BINDING_V1"; context?: never }
  | { schema_version: "REVIEW_PREPARATION_BINDING_V2"; context: ReviewResearchContext }
  | { schema_version: "REVIEW_PREPARATION_BINDING_V3"; context: ReviewResearchContext; analysis_unit: AnalysisUnit });
export type OfflineReviewResult = {
  execution_id: string; preparation_id: string; receipt_id: string; actor_id: string; created_at: string;
  state: "RESULT_SAVED"; mode: "OFFLINE_SYNTHETIC"; adapter_id: "xrml_offline_review"; adapter_version: "1";
};
export type OfflineReviewCapability = {
  enabled: boolean; mode: "OFFLINE_SYNTHETIC"; adapter_id: "xrml_offline_review"; adapter_version: "1";
  reason: null | "review_offline_disabled"; preparation_digest: string; latest: OfflineReviewResult | null;
};
export type OfflineReviewRequest = { request_id: string; expected_preparation_digest: string };
export const preparationRoot = (topic: string, version: string) => `/api/topics/${encodeURIComponent(topic)}/paper-bank/${encodeURIComponent(version)}/review-preparations`;
export const getPreparationSources = (root: string) => fetchJson<PreparationSources>(`${root}/sources`);
export const previewPreparationSource = (root: string, body: PreparationPreviewRequest, signal: AbortSignal) => fetchJson<PreparationPreview>(`${root}/preview`, { method: "POST", body: JSON.stringify(body), signal });
export const listPreparations = (root: string, page: number) => fetchJson<PreparationList>(`${root}?page=${page}&pageSize=10`);
export const getPreparation = (root: string, id: string) => fetchJson<ReviewPreparationRecord>(`${root}/${encodeURIComponent(id)}`);
export const savePreparation = (root: string, body: PreparationRequest) => fetchJson<ReviewPreparationRecord>(root, { method: "POST", body: JSON.stringify(body) });
export const getOfflineReview = (root: string, id: string) => fetchJson<OfflineReviewCapability>(`${root}/${encodeURIComponent(id)}/offline-review`);
export const createOfflineReview = (root: string, id: string, body: OfflineReviewRequest) => fetchJson<OfflineReviewResult>(`${root}/${encodeURIComponent(id)}/offline-reviews`, { method: "POST", body: JSON.stringify(body) });

export type SourceRange = { start: number; end: number };
export type ReviewInputOrigin = "FIXTURE_SYNTHETIC" | "PROVIDER_RESPONSE" | "MANUAL_IMPORT_UNVERIFIED";
type RangeGroup<T> = { record_count: number; covered_chars: number; ranges: SourceRange[]; records: T[] };
export type ReviewSourceCoverageRecord = {
  schema_version: "PAPER_REVIEW_SOURCE_COVERAGE_V1"; anchor_preparation_id: string;
  source: ReviewPreparationRecord["source"]; normalization: "XRML_WHITESPACE_V1";
  decoding: "UTF8_STRICT" | "UTF8_SIG_STRICT";
  prepared: RangeGroup<SourceRange & { preparation_id: string }> & { gaps: SourceRange[] };
  inputs: (RangeGroup<SourceRange & { preparation_id: string; receipt_id: string }> & { input_origin: ReviewInputOrigin })[];
  unmapped_receipt_count: number; other_source_receipt_count: number;
  full_document_review: "NOT_ESTABLISHED"; scientific_support: "NOT_ASSESSED";
  results?: SavedResultRanges;
};
export const getReviewSourceCoverage = (root: string, id: string) => fetchJson<ReviewSourceCoverageRecord>(`${root}/${encodeURIComponent(id)}/coverage`);

export type LocalPdf = { run_id: string; paper_id: string; pdf_sha256: string; pdf_bytes: number; attachment_id?: string };
export type PdfExtractionRequest = { run_id: string; paper_id: string; expected_pdf_sha256: string; attachment_id?: string };
export type PdfExtractionRecord = {
  schema_version: "PAPER_PDF_EXTRACTION_V1" | "PAPER_PDF_EXTRACTION_V2"; extraction_id: string; topic_id: string; paper_version_id: string;
  created_by: string; created_at: string; policy_version: "LOCAL_PDF_TEXT_V1";
  pdf: { sha256: string; bytes: number; artifact_ref: string; attachment_id?: string };
  source: PreparationSource & { artifact_ref: string };
  provenance: {
    schema_version: "PDF_EXTRACTION_PROVENANCE_V1"; source_pdf: { sha256: string; bytes: number };
    extractor: { name: string; version: string | null; normalization: "XRML_WHITESPACE_V1" };
    max_chars: number; document_page_count: number | null; visited_page_numbers: number[]; retained_page_numbers: number[];
    truncation: "TRUNCATED" | "NOT_OBSERVED" | "UNKNOWN"; document_text_chars: number | null;
    outputs: { text_sha256: string; text_bytes: number; pages_sha256: string; pages_bytes: number };
  };
  page_ranges: (SourceRange & { page_number: number; chars: number })[];
  no_text_page_numbers: number[]; unvisited_page_numbers: number[] | null;
  execution: { started: false; full_document_review: "NOT_ESTABLISHED" }; scientific_support: "NOT_ASSESSED";
};
export type PdfExtractionInventory = {
  schema_version: "PAPER_PDF_EXTRACTION_INVENTORY_V1"; pdfs: LocalPdf[]; extractions: PdfExtractionRecord[];
  unavailable: { diagnostic_id: string; run_id: string; reason: string }[];
  limits: { max_pdf_bytes: number; max_source_chars: number; max_artifact_bytes: number; max_document_pages: number }; state: "NOT_EXECUTED";
};
export const getPdfExtractions = (root: string) => fetchJson<unknown>(`${root}/pdf-extractions`);
export const createPdfExtraction = (root: string, body: PdfExtractionRequest) => fetchJson<unknown>(`${root}/pdf-extractions`, { method: "POST", body: JSON.stringify(body) });
export const getPdfExtraction = (root: string, id: string) => fetchJson<unknown>(`${root}/pdf-extractions/${encodeURIComponent(id)}`);

export type ResultRangeRecord = SourceRange & {
  preparation_id: string; receipt_id: string;
  class: "BODY_GROUNDED" | "OTHER_RETURNED" | "UNVERIFIED_PROVIDER";
  subtype: "NATIVE_ANTHROPIC_V2" | "VALIDATED_PREPARED_RESPONSE" | "UNVERIFIED_ORIGIN";
  source: ReviewPreparationRecord["source"]; selection: Omit<ReviewPreparationRecord["selection"], "text">;
  mapping?: "DIRECT_SOURCE_COORDINATES" | "EXACT_UNIQUE_TEXT_MATCH";
};
export type SavedResultRanges = {
  validated_returned: RangeGroup<ResultRangeRecord> & { gaps: SourceRange[] };
  body_grounded: RangeGroup<ResultRangeRecord> & { gaps: SourceRange[] };
  other_returned: RangeGroup<ResultRangeRecord>; unverified_provider: RangeGroup<ResultRangeRecord>;
  gap_semantics: "NO_VALIDATED_SAVED_RESULT"; attempt_history_included: false;
};
export type DocumentCoverageRecord = {
  schema_version: "PAPER_REVIEW_DOCUMENT_COVERAGE_V1"; extraction: PdfExtractionRecord;
  normalization: "XRML_WHITESPACE_V1";
  coordinate_space: "NORMALIZED_EXTRACTION_CODEPOINTS";
  prepared: RangeGroup<SourceRange & { preparation_id: string; source: ReviewPreparationRecord["source"]; selection: Omit<ReviewPreparationRecord["selection"], "text">; mapping: "DIRECT_SOURCE_COORDINATES" | "EXACT_UNIQUE_TEXT_MATCH" }> & { gaps: SourceRange[] };
  results: SavedResultRanges;
  pages: (SourceRange & { page_number: number; chars: number; text_status: "TEXT_EXTRACTED"; prepared_chars: number; body_result_chars: number; body_result_ranges: SourceRange[]; gaps: SourceRange[] })[];
  no_text_page_numbers: number[]; unvisited_page_numbers: number[] | null;
  unaligned: { preparations: UnalignedDocumentRecord[]; receipts: UnalignedDocumentRecord[] };
  full_document_review: "NOT_ESTABLISHED"; scientific_support: "NOT_ASSESSED";
  alignment_claim: "EXACT_NORMALIZED_TEXT_ONLY_NOT_PDF_IDENTITY";
};
export type UnalignedDocumentRecord = {
  preparation_id?: string | null; receipt_id?: string; input_origin?: string; reason: string;
  source?: ReviewPreparationRecord["source"]; selection?: Omit<ReviewPreparationRecord["selection"], "text">;
};
export const getDocumentCoverage = (root: string, id: string) => fetchJson<unknown>(`${root}/pdf-extractions/${encodeURIComponent(id)}/coverage`);
