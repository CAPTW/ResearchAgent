import { buildQuery, fetchJson } from "../lib/api";

export type PaperResourceState = {
  state: "available" | "empty" | "unavailable";
  reason: string | null;
};

export type PaperSourceIdentity = {
  provider: string;
  nativeId: string | null;
  doi: string | null;
};

export type PaperOccurrence = {
  runId: string;
  paperId: string;
  metadataDigest?: string;
  observedAt?: string | null;
};

export type PaperCompatibility = {
  runId: string;
  paperId: string;
  occurrences: PaperOccurrence[];
};

export type PaperReviewProjection = {
  state: "not_reviewed" | "abstract_reviewed" | "full_text_available" | "full_text_reviewed" | "unavailable";
  updatedAt: string | null;
  evidenceCount: number;
};

export type PaperAccessState = "open_access" | "restricted" | "declared" | "unknown";

export type PaperFacetOption = {
  value: string;
  label: string;
  count: number;
};

export type PaperFacet = {
  state: "available" | "empty" | "unavailable";
  options: PaperFacetOption[];
  selected: string | null;
  unknownCount: number;
  reason: string | null;
};

export type PaperBankItem = {
  canonicalPaperId: string;
  paperVersionId: string;
  sourceIdentity: PaperSourceIdentity;
  compatibility: PaperCompatibility;
  metadataDigest: string;
  title: string;
  authors: string[];
  publishedAt: string | null;
  sourceProvider: string;
  sourceUrl: string | null;
  sourceUrlState: "available" | "unavailable";
  theme: string | null;
  publicationYear?: number | null;
  publicationYearState?: "known" | "unknown";
  accessState?: PaperAccessState;
  accessLabel?: string | null;
  review: PaperReviewProjection;
  collectionIds: string[];
};

export type PaperBankResponse = {
  topicId: string;
  items: PaperBankItem[];
  total: number;
  returnedCount: number;
  page: number;
  pageSize: number;
  totalPages: number;
  canonicalQuery: {
    topicId: string;
    q: string;
    provider: string | null;
    collectionId: string | null;
    year?: string | null;
    reviewState: string | null;
    openAccess?: string | null;
    sort: string;
    page?: number;
    pageSize?: number;
  };
  queryDigest: string;
  providerCounts: Array<{ provider: string; label: string; paperCount: number }>;
  reviewResource: PaperResourceState;
  reviewSummary: {
    notReviewed: number;
    abstractReviewed: number;
    fullTextAvailable: number;
    fullTextReviewed: number;
  } | null;
  resource: PaperResourceState;
  collectionResource: PaperResourceState;
  facets?: {
    year: PaperFacet;
    reviewLevel: PaperFacet;
    access: PaperFacet;
    rights: PaperFacet;
  };
  unsupportedFilters: Array<{ id: string; state: "unavailable"; reason: string }>;
};

export type PaperBankCollectionMember = {
  canonicalPaperId: string;
  paperVersionId: string;
  addedAt: string | null;
};

export type PaperBankCollection = {
  id: string;
  name: string;
  isDefault: boolean;
  memberCount: number;
  members: PaperBankCollectionMember[];
  createdAt: string | null;
  updatedAt: string | null;
};

export type PaperBankCollectionList = {
  topicId: string;
  items: PaperBankCollection[];
  resource: PaperResourceState;
};

export type PaperBankEvidence = {
  id: string;
  locator: Record<string, unknown> | null;
  contentDigest: string | null;
  text: string | null;
  destination: string | null;
  destinationState: "available" | "unavailable";
};

export type PaperBankDetail = PaperBankItem & {
  topicId: string;
  abstract: string | null;
  abstractKind: "provider_abstract" | "unavailable";
  fullText: { state: string; artifactRef: string | null; contentDigest: string | null };
  rights: { state: PaperAccessState; label: string | null };
  evidence: PaperBankEvidence[];
  evidenceResource: PaperResourceState;
  provenance: { runIds: string[]; observedAt: string | null };
};

export type PaperBankExport = {
  exportId: string;
  topicId: string;
  canonicalQuery: PaperBankResponse["canonicalQuery"];
  queryDigest: string;
  rowCount: number;
  paperVersionIds: string[];
  generatedAt: string;
  format: "csv";
  mediaType: string;
  fileName: string;
  contentSha256: string;
  artifactRef: string;
  downloadUrl: string;
};

export type PaperBulkSaveResult = {
  collection: PaperBankCollection;
  requestedCount: number;
  uniqueCount: number;
  addedCount: number;
  alreadyPresentCount: number;
  finalMemberCount: number;
  destinationCollectionId: string;
};

export type PaperBankQuery = {
  q?: string;
  provider?: string;
  collectionId?: string;
  reviewState?: string;
  year?: string;
  openAccess?: string;
  sort?: string;
  page?: number;
  pageSize?: number;
};

export function getTopicPaperBank(topicId: string, query: PaperBankQuery = {}) {
  return fetchJson<PaperBankResponse>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-bank${buildQuery(query)}`,
  );
}

export function getTopicPaperDetail(topicId: string, paperVersionId: string) {
  return fetchJson<PaperBankDetail>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-bank/${encodeURIComponent(paperVersionId)}`,
  );
}

export type ReviewInputScope = {
  schema_version: string;
  source?: { run_id: string; paper_id: string; recorded_source_sha256: string | null; preserved_text_sha256: string; preserved_text_chars: number | null; document_chars: number | null; coordinate_space?: "PREPARED_SELECTION_CODEPOINTS" };
  components?: Array<{ json_pointer: string; text_sha256: string; chars: number; source_start: number | null; source_end: number | null; mapping: "exact" | "unknown" }>;
  covered_stored_chars?: number;
  document_coverage?: string; full_document_review?: string; packet_payload_sha256?: string;
};
export type ReviewInputSummary = {
  receipt_id: string; recorded_at: string; input_origin: "FIXTURE_SYNTHETIC" | "PROVIDER_RESPONSE" | "MANUAL_IMPORT_UNVERIFIED";
  run_id: string; paper_id: string; source_text_sha256: string | null; review_level: string; validation_status: string;
  input_scope: ReviewInputScope;
};
export type ReviewInputList = {
  topicId: string; paperVersionId: string; page: number; pageSize: number; hasMore: boolean; items: ReviewInputSummary[];
};
export type ReviewInputDetail = ReviewInputSummary & { packet: Record<string, unknown> & { source_preparation?: import("../lib/reviewPreparation").ReviewPreparationBinding }; draft: Record<string, unknown>; validation?: Record<string, unknown> };
function reviewInputRoot(topicId: string, paperVersionId: string) {
  return `/api/topics/${encodeURIComponent(topicId)}/paper-bank/${encodeURIComponent(paperVersionId)}/review-inputs`;
}
export function getReviewInputs(topicId: string, paperVersionId: string, page: number) {
  return fetchJson<ReviewInputList>(`${reviewInputRoot(topicId, paperVersionId)}?page=${page}&pageSize=10`);
}
export function getReviewInput(topicId: string, paperVersionId: string, receiptId: string) {
  return fetchJson<ReviewInputDetail>(`${reviewInputRoot(topicId, paperVersionId)}/${encodeURIComponent(receiptId)}`);
}

export function getPaperCollections(topicId: string) {
  return fetchJson<PaperBankCollectionList>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections`,
  );
}

export function createPaperCollection(topicId: string, name: string) {
  return fetchJson<PaperBankCollection>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections`,
    { method: "POST", body: JSON.stringify({ name }) },
  );
}

export function renamePaperCollection(topicId: string, collectionId: string, name: string) {
  return fetchJson<PaperBankCollection>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections/${encodeURIComponent(collectionId)}`,
    { method: "PATCH", body: JSON.stringify({ name }) },
  );
}

export function deletePaperCollection(topicId: string, collectionId: string) {
  return fetchJson<{ id: string; deleted: boolean }>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections/${encodeURIComponent(collectionId)}`,
    { method: "DELETE" },
  );
}

export function addPaperToCollection(topicId: string, collectionId: string, paperVersionId: string) {
  return fetchJson<PaperBankCollection>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections/${encodeURIComponent(collectionId)}/members/${encodeURIComponent(paperVersionId)}`,
    { method: "PUT" },
  );
}

export function removePaperFromCollection(topicId: string, collectionId: string, paperVersionId: string) {
  return fetchJson<PaperBankCollection>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections/${encodeURIComponent(collectionId)}/members/${encodeURIComponent(paperVersionId)}`,
    { method: "DELETE" },
  );
}

export function bulkSavePapersToCollection(topicId: string, collectionId: string, selectedPaperVersionIds: string[]) {
  return fetchJson<PaperBulkSaveResult>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-collections/${encodeURIComponent(collectionId)}/members:bulk`,
    { method: "POST", body: JSON.stringify({ selectedPaperVersionIds }) },
  );
}

export function exportTopicPaperBank(
  topicId: string,
  query: {
    q?: string;
    provider?: string;
    collectionId?: string;
    year?: string;
    reviewState?: string;
    openAccess?: string;
    sort?: string;
    page?: number;
    pageSize?: number;
  } = {},
) {
  return fetchJson<PaperBankExport>(
    `/api/topics/${encodeURIComponent(topicId)}/paper-bank/export`,
    { method: "POST", body: JSON.stringify(query) },
  );
}
