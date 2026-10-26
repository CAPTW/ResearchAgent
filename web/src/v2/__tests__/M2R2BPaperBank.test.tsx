import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { getTopicPaperDetail, type PaperBankCollectionList, type PaperBankDetail, type PaperBankResponse } from "../paperBankApi";
import { parsePaperWorkspaceRoute } from "../paperWorkspaceState";
import { V2PapersView } from "../views/V2PapersView";

vi.mock("../paperBankApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../paperBankApi")>();
  return { ...actual, getTopicPaperDetail: vi.fn() };
});

const bank = {
  topicId: "topic-r2b",
  items: [
    {
      canonicalPaperId: "paper-open",
      paperVersionId: "version-open",
      sourceIdentity: { provider: "arxiv", nativeId: "2608.00001v1", doi: null },
      compatibility: { runId: "run-1", paperId: "2608.00001v1", occurrences: [] },
      metadataDigest: "a".repeat(64),
      title: "Unicode, \"Quoted\" Paper",
      authors: ["Author, One"],
      publishedAt: "2026-08-20",
      sourceProvider: "arxiv",
      sourceUrl: "https://arxiv.org/abs/2608.00001v1",
      sourceUrlState: "available",
      theme: "R2B",
      publicationYear: 2026,
      publicationYearState: "known",
      accessState: "open_access",
      accessLabel: "CC-BY",
      review: { state: "full_text_reviewed", updatedAt: "2026-08-20", evidenceCount: 1 },
      collectionIds: [],
    },
    {
      canonicalPaperId: "paper-unknown",
      paperVersionId: "version-unknown",
      sourceIdentity: { provider: "crossref", nativeId: "unknown-1", doi: null },
      compatibility: { runId: "run-1", paperId: "unknown-1", occurrences: [] },
      metadataDigest: "b".repeat(64),
      title: "Unknown rights = formula",
      authors: [],
      publishedAt: null,
      sourceProvider: "crossref",
      sourceUrl: null,
      sourceUrlState: "unavailable",
      theme: null,
      publicationYear: null,
      publicationYearState: "unknown",
      accessState: "unknown",
      accessLabel: "권리 미확인",
      review: { state: "abstract_reviewed", updatedAt: "2026-08-19", evidenceCount: 0 },
      collectionIds: [],
    },
  ],
  total: 2,
  returnedCount: 2,
  page: 1,
  pageSize: 1,
  totalPages: 2,
  canonicalQuery: { topicId: "topic-r2b", q: "", provider: null, collectionId: null, year: null, reviewState: null, openAccess: null, sort: "published_desc" },
  queryDigest: "c".repeat(64),
  providerCounts: [{ provider: "arxiv", label: "arXiv", paperCount: 1 }, { provider: "crossref", label: "Crossref", paperCount: 1 }],
  reviewResource: { state: "available", reason: null },
  reviewSummary: { notReviewed: 0, abstractReviewed: 1, fullTextAvailable: 0, fullTextReviewed: 1 },
  collectionResource: { state: "available", reason: null },
  resource: { state: "available", reason: null },
  facets: {
    year: { state: "available", options: [{ value: "2026", label: "2026", count: 1 }, { value: "unknown", label: "연도 미상", count: 1 }], selected: null, unknownCount: 1, reason: null },
    reviewLevel: { state: "available", options: [{ value: "abstract_reviewed", label: "초록 검토", count: 1 }, { value: "full_text_reviewed", label: "전문 검토 완료", count: 1 }], selected: null, unknownCount: 0, reason: null },
    access: { state: "available", options: [{ value: "open_access", label: "Open access", count: 1 }, { value: "unknown", label: "권리 미확인", count: 1 }], selected: null, unknownCount: 1, reason: null },
    rights: { state: "available", options: [{ value: "open_access", label: "Open access", count: 1 }, { value: "unknown", label: "권리 미확인", count: 1 }], selected: null, unknownCount: 1, reason: null },
  },
  unsupportedFilters: [
    { id: "citation", state: "unavailable", reason: "인용 계약 없음" },
    { id: "impact", state: "unavailable", reason: "M3 범위" },
  ],
} satisfies PaperBankResponse;

const collections: PaperBankCollectionList = { topicId: "topic-r2b", items: [{ id: "saved-papers", name: "Saved papers", isDefault: true, memberCount: 0, members: [], createdAt: null, updatedAt: null }], resource: { state: "available", reason: null } };

function renderView(overrides: Partial<React.ComponentProps<typeof V2PapersView>> = {}) {
  const props: React.ComponentProps<typeof V2PapersView> = {
    paperBank: bank,
    collections,
    collectionsState: "available",
    query: "",
    provider: "",
    collectionId: "",
    reviewState: "",
    year: "",
    openAccess: "",
    sort: "published_desc",
    openPaperVersionId: "",
    onSearch: vi.fn(),
    onProviderChange: vi.fn(),
    onCollectionChange: vi.fn(),
    onReviewStateChange: vi.fn(),
    onYearChange: vi.fn(),
    onOpenAccessChange: vi.fn(),
    onSortChange: vi.fn(),
    onPageChange: vi.fn(),
    onOpenPaper: vi.fn(),
    onClosePaper: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<MemoryRouter><V2PapersView {...props} /></MemoryRouter>) };
}

describe("M2 R2B paper bank contracts", () => {
  it("round-trips year, rights and exact evidence route state", () => {
    const parsed = parsePaperWorkspaceRoute(new URLSearchParams("query=spatial&year=2026&openAccess=open_access&review=full_text_reviewed&paper=version-open&evidence=evidence-1"));
    expect(parsed.year).toBe("2026");
    expect(parsed.openAccess).toBe("open_access");
    expect(parsed.openPaperVersionId).toBe("version-open");
    expect(parsed.openEvidenceId).toBe("evidence-1");
  });

  it("renders server facets with truthful review naming and field-backed access badges", async () => {
    const user = userEvent.setup();
    const { props } = renderView();
    expect(screen.getByRole("combobox", { name: "연도 필터" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "2026 (1)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Open access (1)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "본문 처리 기록 (1)" })).toHaveProperty("value", "full_text_reviewed");
    expect(screen.queryAllByText(/전문 검토 완료/)).toHaveLength(0);
    expect(screen.queryByText(/Paper type/i)).toBeNull();
    expect(screen.getByText("CC-BY").className).toContain("is-open_access");
    await user.selectOptions(screen.getByRole("combobox", { name: "연도 필터" }), "2026");
    await user.selectOptions(screen.getByRole("combobox", { name: "접근 권리" }), "unknown");
    await user.selectOptions(screen.getByRole("combobox", { name: "리뷰 상태" }), "abstract_reviewed");
    expect(props.onYearChange).toHaveBeenCalledWith("2026");
    expect(props.onOpenAccessChange).toHaveBeenCalledWith("unknown");
    expect(props.onReviewStateChange).toHaveBeenCalledWith("abstract_reviewed");
  });

  it("sends one typed atomic bulk request and clears selection only after success", async () => {
    const user = userEvent.setup();
    const onBulkSave = vi.fn().mockResolvedValue({ collection: collections.items[0], requestedCount: 2, uniqueCount: 2, addedCount: 2, alreadyPresentCount: 0, finalMemberCount: 2, destinationCollectionId: "saved-papers" });
    renderView({ onBulkSave });
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[1]);
    await user.click(checkboxes[2]);
    expect(screen.getByText("2 선택됨")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "선택 저장" }));
    await waitFor(() => expect(onBulkSave).toHaveBeenCalledWith(["version-open", "version-unknown"], "saved-papers"));
    expect(screen.getByText("0 선택됨")).toBeTruthy();
  });

  it("exposes real export metadata and download only after the server response", async () => {
    const user = userEvent.setup();
    const onExport = vi.fn().mockResolvedValue({ exportId: "paper-bank-abc-def", topicId: "topic-r2b", canonicalQuery: bank.canonicalQuery, queryDigest: "d".repeat(64), rowCount: 2, paperVersionIds: ["version-open", "version-unknown"], generatedAt: "2026-09-02T00:00:00Z", format: "csv", mediaType: "text/csv; charset=utf-8", fileName: "paper-bank.csv", contentSha256: "e".repeat(64), artifactRef: "paper_bank_exports/topic-r2b/x/paper-bank.csv", downloadUrl: "/api/topics/topic-r2b/paper-bank/exports/x/download" });
    renderView({ onExport });
    expect(screen.queryByText(/다운로드/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "CSV 내보내기" }));
    await waitFor(() => expect(onExport).toHaveBeenCalled());
    expect((await screen.findByRole("link", { name: "다운로드 (2개)" })).getAttribute("href")).toBe("/api/topics/topic-r2b/paper-bank/exports/x/download");
  });

  it("restores the exact evidence element and preserves its destination identity", async () => {
    const evidenceDetail: PaperBankDetail = {
      ...bank.items[0],
      topicId: "topic-r2b",
      abstract: "Stored abstract",
      abstractKind: "provider_abstract",
      fullText: { state: "reviewed", artifactRef: "run:raw/paper.txt", contentDigest: "f".repeat(64) },
      rights: { state: "open_access", label: "CC-BY" },
      evidence: [{ id: "evidence-1", locator: { page: 2 }, contentDigest: "1".repeat(64), text: "Exact span", destination: "/topics/topic-r2b/papers?paper=version-open&evidence=evidence-1", destinationState: "available" }],
      evidenceResource: { state: "available", reason: null },
      provenance: { runIds: ["run-1"], observedAt: null },
    };
    vi.mocked(getTopicPaperDetail).mockResolvedValue(evidenceDetail);
    renderView({ openPaperVersionId: "version-open", openEvidenceId: "evidence-1" });
    const detailRegion = await screen.findByRole("region", { name: "논문 상세" });
    const evidence = await within(detailRegion).findByText("evidence-1");
    expect(evidence.closest("li")?.getAttribute("data-evidence-id")).toBe("evidence-1");
    expect(within(detailRegion).getByRole("link", { name: "증거 provenance 열기" }).getAttribute("href")).toBe("/topics/topic-r2b/papers?paper=version-open&evidence=evidence-1");
  });
});
