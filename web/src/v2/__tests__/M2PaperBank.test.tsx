import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type {
  PaperBankCollectionList,
  PaperBankDetail,
  PaperBankResponse,
} from "../paperBankApi";
import { getTopicPaperDetail, getReviewInputs, getReviewInput } from "../paperBankApi";
import { V2PapersView } from "../views/V2PapersView";

vi.mock("../paperBankApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../paperBankApi")>();
  return { ...actual, getTopicPaperDetail: vi.fn(), getReviewInputs: vi.fn(), getReviewInput: vi.fn() };
});

const paperBank = {
  topicId: "topic-a",
  items: [
    {
      canonicalPaperId: "paper-a",
      paperVersionId: "version-a-v2",
      sourceIdentity: { provider: "arxiv", nativeId: "2601.12345v2", doi: null },
      compatibility: {
        runId: "run-a2",
        paperId: "2601.12345v2",
        occurrences: [{ runId: "run-a2", paperId: "2601.12345v2" }],
      },
      metadataDigest: "a".repeat(64),
      title: "Canonical Systems Paper",
      authors: ["Ada Researcher"],
      publishedAt: "2026-02-01T00:00:00Z",
      sourceProvider: "arxiv",
      sourceUrl: "https://arxiv.org/abs/2601.12345v2",
      sourceUrlState: "available",
      theme: "Systems",
      review: { state: "full_text_reviewed", updatedAt: "2026-08-03T00:00:00Z", evidenceCount: 1 },
      collectionIds: [],
    },
  ],
  total: 1,
  returnedCount: 1,
  page: 1,
  pageSize: 25,
  totalPages: 1,
  canonicalQuery: {
    topicId: "topic-a",
    q: "",
    provider: null,
    collectionId: null,
    reviewState: null,
    sort: "published_desc",
  },
  queryDigest: "b".repeat(64),
  providerCounts: [{ provider: "arxiv", label: "arXiv", paperCount: 1 }],
  reviewResource: { state: "available", reason: null },
  reviewSummary: { notReviewed: 0, abstractReviewed: 0, fullTextAvailable: 0, fullTextReviewed: 1 },
  collectionResource: { state: "available", reason: null },
  resource: { state: "available", reason: null },
  unsupportedFilters: [
    { id: "citation", state: "unavailable", reason: "관측된 인용 계약이 없습니다." },
    { id: "impact", state: "unavailable", reason: "영향력 계약은 M3 범위입니다." },
  ],
} satisfies PaperBankResponse;

const collections = {
  topicId: "topic-a",
  items: [
    {
      id: "saved-papers",
      name: "Saved papers",
      isDefault: true,
      memberCount: 0,
      members: [],
      createdAt: null,
      updatedAt: "2026-08-03T00:00:00Z",
    },
    {
      id: "collection-core",
      name: "Core reading",
      isDefault: false,
      memberCount: 0,
      members: [],
      createdAt: "2026-08-03T00:00:00Z",
      updatedAt: "2026-08-03T00:00:00Z",
    },
  ],
  resource: { state: "available", reason: null },
} satisfies PaperBankCollectionList;

const detail = {
  topicId: "topic-a",
  canonicalPaperId: "paper-a",
  paperVersionId: "version-a-v2",
  title: "Canonical Systems Paper",
  authors: ["Ada Researcher"],
  abstract: "A stored abstract.",
  abstractKind: "provider_abstract",
  publishedAt: "2026-02-01T00:00:00Z",
  sourceProvider: "arxiv",
  sourceIdentity: { provider: "arxiv", nativeId: "2601.12345v2", doi: null },
  sourceUrl: "https://arxiv.org/abs/2601.12345v2",
  sourceUrlState: "available",
  theme: "Systems",
  collectionIds: [],
  metadataDigest: "a".repeat(64),
  compatibility: {
    runId: "run-a2",
    paperId: "2601.12345v2",
    occurrences: [{ runId: "run-a2", paperId: "2601.12345v2" }],
  },
  review: { state: "full_text_reviewed", updatedAt: "2026-08-03T00:00:00Z", evidenceCount: 1 },
  fullText: { state: "reviewed", artifactRef: "run:raw/2601.12345v2.txt", contentDigest: "c".repeat(64) },
  rights: { state: "unknown", label: null },
  evidence: [
    {
      id: "evidence-v2-1",
      locator: { page: 4, section: "Results" },
      contentDigest: "d".repeat(64),
      text: "Exact evidence text.",
      destination: null,
      destinationState: "unavailable",
    },
  ],
  evidenceResource: { state: "available", reason: null },
  provenance: { runIds: ["run-a2"], observedAt: null },
} satisfies PaperBankDetail;

function renderView(overrides: Partial<React.ComponentProps<typeof V2PapersView>> = {}) {
  const props: React.ComponentProps<typeof V2PapersView> = {
    paperBank,
    collections,
    collectionsState: "available",
    query: "",
    provider: "",
    collectionId: "",
    reviewState: "",
    sort: "published_desc",
    openPaperVersionId: "",
    onSearch: vi.fn(),
    onProviderChange: vi.fn(),
    onCollectionChange: vi.fn(),
    onReviewStateChange: vi.fn(),
    onSortChange: vi.fn(),
    onPageChange: vi.fn(),
    onOpenPaper: vi.fn(),
    onClosePaper: vi.fn(),
    onSavePaper: vi.fn().mockResolvedValue(undefined),
    onRemovePaper: vi.fn().mockResolvedValue(undefined),
    onCreateCollection: vi.fn().mockResolvedValue(undefined),
    onRenameCollection: vi.fn().mockResolvedValue(undefined),
    onDeleteCollection: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  return {
    props,
    ...render(<MemoryRouter><V2PapersView {...props} /></MemoryRouter>),
  };
}

describe("M2 paper bank workspace", () => {
  it("renders a semantic real paper table and calls server-backed query/filter/page controls", async () => {
    const user = userEvent.setup();
    const { props } = renderView();

    expect(screen.getByRole("table", { name: "논문 목록" })).toBeTruthy();
    expect(screen.getByRole("row", { name: /Canonical Systems Paper/ })).toBeTruthy();
    expect(screen.queryAllByText("전문 검토 완료")).toHaveLength(0);
    expect(within(screen.getByRole("row", { name: /Canonical Systems Paper/ })).getByText("본문 처리 기록")).toBeTruthy();
    expect(screen.getByText(/AI가 논문 전체를 검토했다는 뜻은 아닙니다/)).toBeTruthy();
    await user.type(screen.getByRole("textbox", { name: "논문 제목, 저자, 키워드 검색" }), "systems{Enter}");
    expect(props.onSearch).toHaveBeenCalledWith("systems");
    await user.selectOptions(screen.getByRole("combobox", { name: "출처" }), "arxiv");
    expect(props.onProviderChange).toHaveBeenCalledWith("arxiv");
    await user.selectOptions(screen.getByRole("combobox", { name: "표시할 컬렉션" }), "saved-papers");
    expect(props.onCollectionChange).toHaveBeenCalledWith("saved-papers");
    await user.selectOptions(screen.getByRole("combobox", { name: "리뷰 상태" }), "full_text_reviewed");
    expect(props.onReviewStateChange).toHaveBeenCalledWith("full_text_reviewed");
    const pagination = screen.getByRole("navigation", { name: "논문 페이지 탐색" });
    const currentPage = within(pagination).getByText(/^1 \/ 1/);
    expect(currentPage.getAttribute("aria-current")).toBe("page");
    expect((within(pagination).getByRole("button", { name: "이전 페이지" }) as HTMLButtonElement).disabled).toBe(true);
    const nextPage = within(pagination).getByRole("button", { name: "다음 페이지" }) as HTMLButtonElement;
    expect(nextPage.disabled).toBe(true);
    await user.click(nextPage);
    expect(props.onPageChange).not.toHaveBeenCalled();
  });

  it("persists a row bookmark only after the mutation resolves and keeps Add to report unavailable", async () => {
    const user = userEvent.setup();
    const onSavePaper = vi.fn().mockResolvedValue(undefined);
    renderView({ onSavePaper });

    await user.click(screen.getByRole("button", { name: "Canonical Systems Paper 저장" }));
    await waitFor(() => expect(onSavePaper).toHaveBeenCalledWith("version-a-v2", "saved-papers"));
    expect(screen.getByRole("status").textContent).toContain("저장했습니다");
    const reportButton = screen.getByRole("button", { name: /선택 보고서/ }) as HTMLButtonElement;
    expect(reportButton.disabled).toBe(true);
    expect(reportButton.title).toContain("고정 입력 API가 연결되지 않았습니다");
  });

  it("separates the named collection save target from the result collection filter", async () => {
    const user = userEvent.setup();
    const onSavePaper = vi.fn().mockResolvedValue(undefined);
    const { props } = renderView({ collectionId: "", onSavePaper });

    await user.selectOptions(screen.getByRole("combobox", { name: "저장 대상 컬렉션" }), "collection-core");
    await user.click(screen.getByRole("button", { name: "Canonical Systems Paper 저장" }));

    await waitFor(() => expect(onSavePaper).toHaveBeenCalledWith("version-a-v2", "collection-core"));
    expect(props.onCollectionChange).not.toHaveBeenCalled();
  });

  it("truthfully discloses fallback when the selected save target disappears", async () => {
    const user = userEvent.setup();
    const { props, rerender } = renderView();
    await user.selectOptions(screen.getByRole("combobox", { name: "저장 대상 컬렉션" }), "collection-core");

    rerender(
      <MemoryRouter><V2PapersView
        {...props}
        collections={{ ...collections, items: collections.items.filter((item) => item.id !== "collection-core") }}
      /></MemoryRouter>,
    );

    expect((screen.getByRole("combobox", { name: "저장 대상 컬렉션" }) as HTMLSelectElement).value).toBe("saved-papers");
    expect((await screen.findByRole("status")).textContent).toContain("Saved papers");
  });

  it("removes an existing exact membership and rolls back truthful notice on mutation failure", async () => {
    const user = userEvent.setup();
    const onRemovePaper = vi.fn().mockResolvedValue(undefined);
    const savedBank = {
      ...paperBank,
      items: [{ ...paperBank.items[0], collectionIds: ["saved-papers"] }],
    } satisfies PaperBankResponse;
    const { rerender, props } = renderView({ paperBank: savedBank, onRemovePaper });

    await user.click(screen.getByRole("button", { name: "Canonical Systems Paper 저장 해제" }));
    await waitFor(() => expect(onRemovePaper).toHaveBeenCalledWith("version-a-v2", "saved-papers"));
    expect(screen.getByRole("status").textContent).toContain("제거했습니다");

    const failingRemove = vi.fn().mockRejectedValue(new Error("collection_write_failed"));
    rerender(<MemoryRouter><V2PapersView {...props} paperBank={savedBank} onRemovePaper={failingRemove} /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Canonical Systems Paper 저장 해제" }));
    expect(await screen.findByText("collection_write_failed")).toBeTruthy();
    expect(screen.queryByText(/제거했습니다/)).toBeNull();
  });

  it("manages named collections and distinguishes unavailable from empty", async () => {
    const user = userEvent.setup();
    const onCreateCollection = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderView({ onCreateCollection });
    await user.type(screen.getByRole("textbox", { name: "새 컬렉션 이름" }), "Methods");
    await user.click(screen.getByRole("button", { name: "컬렉션 생성" }));
    expect(onCreateCollection).toHaveBeenCalledWith("Methods");

    rerender(
      <MemoryRouter><V2PapersView
        {...renderView().props}
        collections={null}
        collectionsState="unavailable"
        collectionsReason="paper_collection_store_unavailable"
      /></MemoryRouter>,
    );
    expect(screen.getByText("컬렉션을 사용할 수 없습니다.")).toBeTruthy();
    expect(screen.getByText("paper_collection_store_unavailable")).toBeTruthy();
  });

  it("restores a deep-linked detail, preserves evidence identity, handles Escape, and restores focus", async () => {
    vi.mocked(getTopicPaperDetail).mockResolvedValue(detail);
    const user = userEvent.setup();
    const onOpenPaper = vi.fn();
    const onClosePaper = vi.fn();
    const { props, rerender } = renderView({ onOpenPaper, onClosePaper });
    const titleButton = screen.getByRole("button", { name: "Canonical Systems Paper 상세 열기" });
    titleButton.focus();
    await user.click(titleButton);
    expect(onOpenPaper).toHaveBeenCalledWith("version-a-v2");

    rerender(
      <MemoryRouter><V2PapersView
        {...props}
        onClosePaper={onClosePaper}
        openPaperVersionId="version-a-v2"
      /></MemoryRouter>,
    );
    const detailRegion = await screen.findByRole("region", { name: "논문 상세" });
    expect(within(detailRegion).getByText("evidence-v2-1")).toBeTruthy();
    expect(within(detailRegion).getByText("dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")).toBeTruthy();
    fireEvent.keyDown(detailRegion, { key: "Escape" });
    expect(onClosePaper).toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(titleButton));
  });

  it("distinguishes an unavailable evidence resource from a valid empty evidence result", async () => {
    const unavailable = {
      ...detail,
      evidence: [],
      evidenceResource: { state: "unavailable", reason: "paper_review_store_unavailable" },
    } satisfies PaperBankDetail;
    vi.mocked(getTopicPaperDetail).mockResolvedValue(unavailable);
    renderView({ openPaperVersionId: "version-a-v2" });

    const detailRegion = await screen.findByRole("region", { name: "논문 상세" });
    expect(within(detailRegion).getByText("Evidence를 사용할 수 없습니다.")).toBeTruthy();
    expect(within(detailRegion).getByText("paper_review_store_unavailable")).toBeTruthy();
    expect(within(detailRegion).queryByText("연결된 evidence가 없습니다.")).toBeNull();
  });

  it("exposes unsupported filters as disabled controls with assistive reasons", () => {
    renderView();
    const citation = screen.getByRole("combobox", { name: "인용수 필터" }) as HTMLSelectElement;
    const impact = screen.getByRole("combobox", { name: "영향력 필터" }) as HTMLSelectElement;
    expect(citation.disabled).toBe(true);
    expect(impact.disabled).toBe(true);
    expect(citation.title).toContain("관측된 인용 계약");
    expect(impact.title).toContain("M3");
  });

  it("freezes exact selected versions with their observed digests and reuses the retry identity", async () => {
    const freeze=vi.fn().mockRejectedValue(new Error("response lost"));
    renderView({onFreezeSelection:freeze});
    fireEvent.click(screen.getByRole("checkbox",{name:"Canonical Systems Paper 선택"}));
    fireEvent.click(screen.getByRole("button",{name:"선택 보고서 입력 고정"}));
    await screen.findByText("response lost");
    fireEvent.click(screen.getByRole("button",{name:"선택 보고서 입력 고정"}));
    await waitFor(()=>expect(freeze).toHaveBeenCalledTimes(2));
    expect(freeze.mock.calls[0]).toEqual(freeze.mock.calls[1]);
    expect(freeze.mock.calls[0][0]).toEqual([{paper_version_id:"version-a-v2",metadata_digest:"a".repeat(64)}]);
    expect(freeze.mock.calls[0][1]).toMatch(/^[a-f0-9-]{36}$/);
  });

  it("keeps observed metadata when select-all adds new papers after a list refresh", async () => {
    const freeze = vi.fn().mockResolvedValue(undefined);
    const { props, rerender } = renderView({ onFreezeSelection: freeze });
    fireEvent.click(screen.getByRole("checkbox", { name: "Canonical Systems Paper 선택" }));
    const refreshed = {
      ...paperBank,
      items: [
        { ...paperBank.items[0], metadataDigest: "d".repeat(64) },
        { ...paperBank.items[0], paperVersionId: "version-b-v1", title: "Another paper", metadataDigest: "b".repeat(64) },
      ],
    } satisfies PaperBankResponse;
    rerender(<MemoryRouter><V2PapersView {...props} paperBank={refreshed} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("checkbox", { name: "전체 선택" }));
    fireEvent.click(screen.getByRole("button", { name: "선택 보고서 입력 고정" }));
    await waitFor(() => expect(freeze).toHaveBeenCalledTimes(1));
    expect(freeze.mock.calls[0][0]).toEqual([
      { paper_version_id: "version-a-v2", metadata_digest: "a".repeat(64) },
      { paper_version_id: "version-b-v1", metadata_digest: "b".repeat(64) },
    ]);
  });

  it("preserves selections from other pages and the metadata actually selected", async () => {
    const freeze=vi.fn().mockResolvedValue(undefined);
    const {props,rerender}=renderView({onFreezeSelection:freeze});
    fireEvent.click(screen.getByRole("checkbox",{name:"전체 선택"}));
    const second={...paperBank,page:2,items:[{...paperBank.items[0],paperVersionId:"version-a-v3",title:"Next version",metadataDigest:"c".repeat(64)}]};
    rerender(<MemoryRouter><V2PapersView {...props} paperBank={second}/></MemoryRouter>);
    fireEvent.click(screen.getByRole("checkbox",{name:"전체 선택"}));
    expect(screen.getByText("2 선택됨")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox",{name:"전체 선택"}));
    expect(screen.getByText("1 선택됨")).toBeTruthy();
    rerender(<MemoryRouter><V2PapersView {...props} paperBank={{...paperBank,items:[{...paperBank.items[0],metadataDigest:"d".repeat(64)}]}}/></MemoryRouter>);
    fireEvent.click(screen.getByRole("button",{name:"선택 보고서 입력 고정"}));
    await waitFor(()=>expect(freeze).toHaveBeenCalled());
    expect(freeze.mock.calls[0][0]).toEqual([{paper_version_id:"version-a-v2",metadata_digest:"a".repeat(64)}]);
  });

  it("retains an explicitly chosen receipt through capture failure and paging, clears it on deselection", async () => {
    const freeze = vi.fn().mockRejectedValue(new Error("synthesis_review_input_not_found"));
    const receipt = { receipt_id: "saved-review", input_origin: "FIXTURE_SYNTHETIC", recorded_at: "2026-09-28T09:00:00Z", run_id: "run-a2", paper_id: "2601.12345v2", source_text_sha256: "f".repeat(64), review_level: "full_text_grounded", validation_status: "validated", input_scope: { schema_version: "AI_REVIEW_INPUT_SCOPE_V1" }, packet: { packet_version: "ai-review-packet-v2", input_scope: { schema_version: "AI_REVIEW_INPUT_SCOPE_V1", components: [{ json_pointer: "/selected_text_excerpts/0/text", chars: 8 }] }, selected_text_excerpts: [{ text: "Original", char_count: 8 }] }, draft: {} };
    vi.mocked(getTopicPaperDetail).mockResolvedValue(detail);
    vi.mocked(getReviewInputs).mockResolvedValue({ topicId: "topic-a", paperVersionId: "version-a-v2", page: 1, pageSize: 10, hasMore: false, items: [receipt] } as never);
    vi.mocked(getReviewInput).mockResolvedValue(receipt as never);
    const { props, rerender } = renderView({ onFreezeSelection: freeze, openPaperVersionId: "version-a-v2" });
    fireEvent.click(screen.getByText("검토 입력·결과 이력"));
    fireEvent.click(await screen.findByRole("button", { name: /입력·결과 열기/ }));
    fireEvent.click(await screen.findByRole("button", { name: "보고서 입력에 이 기록 선택" }));
    expect(screen.getByText("1 선택됨")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "선택 보고서 입력 고정" }));
    await screen.findByText("synthesis_review_input_not_found");
    fireEvent.click(screen.getByRole("button", { name: "선택 보고서 입력 고정" }));
    await waitFor(() => expect(freeze).toHaveBeenCalledTimes(2));
    expect(freeze.mock.calls[0]).toEqual(freeze.mock.calls[1]);
    expect(freeze.mock.calls[0][0]).toEqual([{ paper_version_id: "version-a-v2", metadata_digest: "a".repeat(64), review_input: { receipt_id: "saved-review", component_ids: ["/selected_text_excerpts/0/text"] } }]);
    rerender(<MemoryRouter><V2PapersView {...props} openPaperVersionId="" paperBank={{ ...paperBank, page: 2, items: [] }}/></MemoryRouter>);
    expect(screen.getByRole("button", { name: "검토 기록 선택 해제 · saved-review" })).toBeTruthy();
    rerender(<MemoryRouter><V2PapersView {...props}/></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "검토 기록 선택 해제 · saved-review" }));
    expect(screen.getByRole("checkbox", { name: "Canonical Systems Paper 선택" })).toHaveProperty("checked", true);
    fireEvent.click(screen.getByRole("button", { name: "선택 보고서 입력 고정" }));
    await waitFor(() => expect(freeze).toHaveBeenCalledTimes(3));
    expect(freeze.mock.calls[2][0]).toEqual([{ paper_version_id: "version-a-v2", metadata_digest: "a".repeat(64) }]);
    expect(freeze.mock.calls[2][1]).not.toBe(freeze.mock.calls[0][1]);
    await screen.findByText("synthesis_review_input_not_found");
    fireEvent.click(screen.getByText("검토 입력·결과 이력"));
    fireEvent.click(await screen.findByRole("button", { name: /입력·결과 열기/ }));
    fireEvent.click(await screen.findByRole("button", { name: "보고서 입력에 이 기록 선택" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Canonical Systems Paper 선택" }));
    expect(screen.queryByRole("button", { name: "검토 기록 선택 해제 · saved-review" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "보고서 입력에 이 기록 선택" }));
    rerender(<MemoryRouter><V2PapersView {...props} openPaperVersionId="" paperBank={{ ...paperBank, topicId: "other-topic" }}/></MemoryRouter>);
    await waitFor(()=>expect(screen.queryByRole("button", { name: "검토 기록 선택 해제 · saved-review" })).toBeNull());
    expect(screen.getByText("0 선택됨")).toBeTruthy();
  });
});
