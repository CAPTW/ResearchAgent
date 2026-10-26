import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthSessionResponse } from "../../lib/api";
import type {
  PaperBankCollectionList,
  PaperBankDetail,
  PaperBankResponse,
} from "../paperBankApi";

const apiMocks = vi.hoisted(() => ({
  captureSynthesis: vi.fn(),
  createTopicProfile: vi.fn(),
  getApiTopicDashboard: vi.fn(),
  getApiTopics: vi.fn(),
  getPaperCollections: vi.fn(),
  getTopicPaperBank: vi.fn(),
  getTopicPaperDetail: vi.fn(),
  getTopicProfiles: vi.fn(),
  runApiTopicAction: vi.fn(),
}));

vi.mock("../../lib/localSynthesis", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/localSynthesis")>()),
  captureSynthesis: apiMocks.captureSynthesis,
}));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  createTopicProfile: apiMocks.createTopicProfile,
  getApiTopicDashboard: apiMocks.getApiTopicDashboard,
  getApiTopics: apiMocks.getApiTopics,
  getTopicProfiles: apiMocks.getTopicProfiles,
  runApiTopicAction: apiMocks.runApiTopicAction,
}));

vi.mock("../paperBankApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../paperBankApi")>()),
  getPaperCollections: apiMocks.getPaperCollections,
  getTopicPaperBank: apiMocks.getTopicPaperBank,
  getTopicPaperDetail: apiMocks.getTopicPaperDetail,
}));

import { V2Shell } from "../V2Shell";
import { V2TopicWorkspaceScreen } from "../V2TopicWorkspaceScreen";

const auth = {
  auth_mode: "disabled" as const,
  authenticated: true,
  is_admin: true,
  user: null,
  login_url: null,
  logout_url: "/auth/logout",
  mcp_url: null,
  mcp_resource_metadata_url: null,
  product_auth_server_side_only: true,
  provider_auth_server_side_only: true,
  product_session_backend: "local_internal",
  warnings: [],
} satisfies AuthSessionResponse;

const collections = {
  topicId: "topic-a",
  items: [
    {
      id: "saved-papers",
      name: "Saved papers",
      isDefault: true,
      memberCount: 1,
      members: [{ canonicalPaperId: "paper-a", paperVersionId: "version-a", addedAt: null }],
      createdAt: null,
      updatedAt: "2026-08-31T00:00:00Z",
    },
    {
      id: "collection-core",
      name: "Core reading",
      isDefault: false,
      memberCount: 1,
      members: [{ canonicalPaperId: "paper-a", paperVersionId: "version-a", addedAt: null }],
      createdAt: "2026-08-31T00:00:00Z",
      updatedAt: "2026-08-31T00:00:00Z",
    },
  ],
  resource: { state: "available", reason: null },
} satisfies PaperBankCollectionList;

function bank(title = "Canonical Systems Paper", collectionId: string | null = null): PaperBankResponse {
  return {
    topicId: "topic-a",
    items: [{
      canonicalPaperId: "paper-a",
      paperVersionId: "version-a",
      sourceIdentity: { provider: "arxiv", nativeId: "2601.12345v2", doi: null },
      compatibility: {
        runId: "run-a",
        paperId: "2601.12345v2",
        occurrences: [{ runId: "run-a", paperId: "2601.12345v2" }],
      },
      metadataDigest: "a".repeat(64),
      title,
      authors: ["Ada Researcher"],
      publishedAt: "2026-02-01T00:00:00Z",
      sourceProvider: "arxiv",
      sourceUrl: null,
      sourceUrlState: "unavailable",
      theme: "Systems",
      review: { state: "full_text_reviewed", updatedAt: "2026-08-31T00:00:00Z", evidenceCount: 1 },
      collectionIds: ["saved-papers", "collection-core"],
    }],
    total: 1,
    returnedCount: 1,
    page: 1,
    pageSize: 25,
    totalPages: 1,
    canonicalQuery: {
      topicId: "topic-a",
      q: "",
      provider: null,
      collectionId,
      reviewState: null,
      sort: "published_desc",
      page: 1,
      pageSize: 25,
    },
    queryDigest: "b".repeat(64),
    providerCounts: [{ provider: "arxiv", label: "arXiv", paperCount: 1 }],
    reviewResource: { state: "available", reason: null },
    reviewSummary: { notReviewed: 0, abstractReviewed: 0, fullTextAvailable: 0, fullTextReviewed: 1 },
    collectionResource: { state: "available", reason: null },
    resource: { state: "available", reason: null },
    unsupportedFilters: [],
  };
}

const detail = {
  ...bank().items[0],
  topicId: "topic-a",
  abstract: "Stored abstract",
  abstractKind: "provider_abstract",
  fullText: { state: "reviewed", artifactRef: "run:raw/paper.txt", contentDigest: "c".repeat(64) },
  rights: { state: "open_access", label: "CC-BY-4.0" },
  evidence: [{
    id: "evidence-a",
    locator: { page: 4 },
    contentDigest: "d".repeat(64),
    text: "Grounded evidence",
    destination: null,
    destinationState: "unavailable",
  }],
  evidenceResource: { state: "available", reason: null },
  provenance: { runIds: ["run-a"], observedAt: "2026-08-31T00:00:00Z" },
} satisfies PaperBankDetail;

function RouteProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <output aria-label="현재 경로">{location.pathname}{location.search}</output>
      <button type="button" onClick={() => navigate(-1)}>브라우저 뒤로</button>
      <button type="button" onClick={() => navigate(1)}>브라우저 앞으로</button>
      <button type="button" onClick={() => navigate("/topics/topic-b/papers")}>다른 주제 논문 열기</button>
    </div>
  );
}

function renderWorkspace(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <RouteProbe />
      <Routes>
        <Route path="/topics/:topicId/:tab" element={<V2TopicWorkspaceScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.getApiTopics.mockResolvedValue({
    items: [{
      id: "topic-a",
      name: "Topic A",
      description: "Research topic",
      updatedAt: "2026-08-31T00:00:00Z",
      status: "active",
      isFavorite: true,
    }],
  });
  apiMocks.getTopicProfiles.mockResolvedValue({ items: [] });
  apiMocks.getApiTopicDashboard.mockResolvedValue({
    topic: { id: "topic-a", name: "Topic A", updatedAt: "2026-08-31T00:00:00Z" },
  });
  apiMocks.getPaperCollections.mockResolvedValue(collections);
  apiMocks.getTopicPaperBank.mockResolvedValue(bank());
  apiMocks.getTopicPaperDetail.mockResolvedValue(detail);
});

describe("R2A Shell Saved papers and collection route", () => {
  it("blocks the previous topic's selected papers while the new topic request is pending", async () => {
    const user = userEvent.setup();
    const nextBank = deferred<PaperBankResponse>();
    const nextCollections = deferred<PaperBankCollectionList>();
    apiMocks.getTopicPaperBank.mockImplementation((topicId: string) => topicId === "topic-b" ? nextBank.promise : Promise.resolve(bank()));
    apiMocks.getPaperCollections.mockImplementation((topicId: string) => topicId === "topic-b" ? nextCollections.promise : Promise.resolve(collections));
    apiMocks.captureSynthesis.mockRejectedValue(new Error("capture_response_lost"));
    renderWorkspace("/topics/topic-a/papers");
    await user.click(await screen.findByRole("checkbox", { name: "Canonical Systems Paper 선택" }));
    expect(screen.getByText("1 선택됨")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "다른 주제 논문 열기" }));
    await waitFor(() => expect(apiMocks.getTopicPaperBank).toHaveBeenLastCalledWith("topic-b", expect.anything()));
    expect(screen.queryByRole("table", { name: "논문 목록" })).toBeNull();
    expect(screen.queryByRole("button", { name: "선택 보고서 입력 고정" })).toBeNull();
    expect(screen.queryByRole("region", { name: "보고서에 선택한 검토 기록" })).toBeNull();
    expect(screen.getByText("논문 목록을 불러오는 중입니다.")).toBeTruthy();
    expect(apiMocks.captureSynthesis).not.toHaveBeenCalled();

    const payload = bank("Topic B shared paper");
    payload.topicId = "topic-b";
    payload.canonicalQuery.topicId = "topic-b";
    payload.items[0].metadataDigest = "e".repeat(64);
    await act(async () => {
      nextBank.resolve(payload);
      nextCollections.resolve({ ...collections, topicId: "topic-b" });
    });
    const checkbox = await screen.findByRole("checkbox", { name: "Topic B shared paper 선택" });
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText("0 선택됨")).toBeTruthy();
    expect((screen.getByRole("button", { name: "선택 보고서 입력 고정" }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: "선택 보고서 입력 고정" }));
    await waitFor(() => expect(apiMocks.captureSynthesis).toHaveBeenCalledExactlyOnceWith(
      "topic-b", [{ paper_version_id: "version-a", metadata_digest: "e".repeat(64) }], expect.any(String),
    ));
    expect(await screen.findByText("capture_response_lost")).toBeTruthy();
  });

  it("coalesces identical in-flight Paper Bank loads during StrictMode effects", async () => {
    const bankRequest = deferred<PaperBankResponse>();
    apiMocks.getTopicPaperBank.mockReturnValue(bankRequest.promise);

    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/topics/topic-a/papers"]}>
          <RouteProbe />
          <Routes>
            <Route path="/topics/:topicId/:tab" element={<V2TopicWorkspaceScreen />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    );

    await waitFor(() => expect(apiMocks.getTopicPaperBank).toHaveBeenCalledTimes(1));
    bankRequest.resolve(bank());
    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });
  });

  it("routes the active Shell affordance to the topic Papers saved alias", async () => {
    render(
      <MemoryRouter initialEntries={["/topics/topic-a/papers?collection=saved"]}>
        <V2Shell auth={auth} onLogout={vi.fn()}>
          <div>workspace</div>
        </V2Shell>
      </MemoryRouter>,
    );

    const link = await screen.findByRole("link", { name: "저장된 논문" });
    expect(link.getAttribute("href")).toBe("/topics/topic-a/papers?collection=saved");
    expect(link.getAttribute("aria-current")).toBe("page");
  });

  it("resolves the saved alias to the real default persisted collection", async () => {
    renderWorkspace("/topics/topic-a/papers?collection=saved");

    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });
    expect(apiMocks.getTopicPaperBank).toHaveBeenLastCalledWith(
      "topic-a",
      expect.objectContaining({ collectionId: "saved-papers" }),
    );
    expect(screen.getByText("컬렉션", { selector: "dt" }).nextElementSibling?.textContent).toBe("Saved papers");
  });

  it("round-trips named and saved collection routes through browser history", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved");

    const filter = await screen.findByRole("combobox", { name: "표시할 컬렉션" });
    await user.selectOptions(filter, "collection-core");
    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=collection-core"));

    await user.click(screen.getByRole("button", { name: "브라우저 뒤로" }));
    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=saved"));

    await user.click(screen.getByRole("button", { name: "브라우저 앞으로" }));
    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=collection-core"));
  });

  it("keeps the explicit Save target independent from collection read navigation", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers");

    const target = await screen.findByRole("combobox", { name: "저장 대상 컬렉션" });
    await user.selectOptions(target, "collection-core");
    await user.selectOptions(screen.getByRole("combobox", { name: "표시할 컬렉션" }), "saved-papers");

    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=saved"));
    expect((screen.getByRole("combobox", { name: "저장 대상 컬렉션" }) as HTMLSelectElement).value).toBe("collection-core");
  });

  it("distinguishes a missing named collection and offers a real clear-filter action", async () => {
    apiMocks.getTopicPaperBank.mockRejectedValueOnce(new Error("collection_not_found"));
    renderWorkspace("/topics/topic-a/papers?collection=collection-deleted");

    expect(await screen.findByText("요청한 컬렉션을 찾을 수 없습니다.")).toBeTruthy();
    const clear = screen.getByRole("button", { name: "컬렉션 필터 지우기" });
    await userEvent.click(clear);
    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toBe("/topics/topic-a/papers"));
  });

  it("renders a true empty saved collection separately from unavailable collection state", async () => {
    apiMocks.getPaperCollections.mockResolvedValueOnce({
      ...collections,
      items: [{ ...collections.items[0], memberCount: 0, members: [] }],
    });
    apiMocks.getTopicPaperBank.mockResolvedValueOnce({
      ...bank("", "saved-papers"),
      items: [],
      total: 0,
      returnedCount: 0,
      totalPages: 0,
    });
    renderWorkspace("/topics/topic-a/papers?collection=saved");

    expect(await screen.findByText("Saved papers 컬렉션은 존재하며 저장된 논문이 0개입니다.")).toBeTruthy();
    expect(screen.queryByText("컬렉션 리소스를 사용할 수 없습니다.")).toBeNull();
  });

  it("preserves collection intent while opening and closing exact paper detail", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved");

    await user.click(await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" }));
    await screen.findByRole("region", { name: "논문 상세" });
    expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=saved");
    expect(screen.getByLabelText("현재 경로").textContent).toContain("paper=version-a");
    expect(await screen.findByText("evidence-a")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "논문 상세 닫기" }));
    await waitFor(() => expect(screen.getByLabelText("현재 경로").textContent).toBe("/topics/topic-a/papers?collection=saved"));
  });
});

describe("R2A resource-scoped Papers refresh", () => {
  it("refreshes Paper Bank and collections without reloading base metadata or creating work", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved&provider=arxiv&sort=title_asc");

    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });
    const baseCalls = apiMocks.getApiTopicDashboard.mock.calls.length;
    const bankCalls = apiMocks.getTopicPaperBank.mock.calls.length;
    const collectionCalls = apiMocks.getPaperCollections.mock.calls.length;
    apiMocks.getTopicPaperBank.mockResolvedValueOnce(bank("Refreshed Systems Paper", "saved-papers"));

    await user.click(screen.getByRole("button", { name: /새로고침/ }));

    expect(await screen.findByRole("button", { name: "Refreshed Systems Paper 상세 열기" })).toBeTruthy();
    expect(await screen.findByText("논문 리소스를 새로고침했습니다.")).toBeTruthy();
    expect(apiMocks.getTopicPaperBank).toHaveBeenCalledTimes(bankCalls + 1);
    expect(apiMocks.getPaperCollections).toHaveBeenCalledTimes(collectionCalls + 1);
    expect(apiMocks.getApiTopicDashboard).toHaveBeenCalledTimes(baseCalls);
    expect(apiMocks.runApiTopicAction).not.toHaveBeenCalled();
    expect(screen.getByLabelText("현재 경로").textContent).toBe(
      "/topics/topic-a/papers?collection=saved&provider=arxiv&sort=title_asc",
    );
  });

  it("fences duplicate Refresh activation while exposing busy state", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved");
    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });
    const pendingBank = deferred<PaperBankResponse>();
    const pendingCollections = deferred<PaperBankCollectionList>();
    const baseline = apiMocks.getTopicPaperBank.mock.calls.length;
    apiMocks.getTopicPaperBank.mockImplementationOnce(() => pendingBank.promise);
    apiMocks.getPaperCollections.mockImplementationOnce(() => pendingCollections.promise);

    const refresh = screen.getByRole("button", { name: /새로고침/ });
    await user.click(refresh);
    expect(refresh.getAttribute("aria-busy")).toBe("true");
    await user.click(refresh);
    expect(apiMocks.getTopicPaperBank).toHaveBeenCalledTimes(baseline + 1);

    await act(async () => {
      pendingBank.resolve(bank("After Busy Refresh", "saved-papers"));
      pendingCollections.resolve(collections);
      await pendingBank.promise;
      await pendingCollections.promise;
    });
    expect(await screen.findByRole("button", { name: "After Busy Refresh 상세 열기" })).toBeTruthy();
  });

  it("keeps successful Paper Bank data and prior collections when collection refresh fails", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved");
    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });
    apiMocks.getTopicPaperBank.mockResolvedValueOnce(bank("Bank Survives Collection Failure", "saved-papers"));
    apiMocks.getPaperCollections.mockRejectedValueOnce(new Error("collection_store_unavailable"));

    await user.click(screen.getByRole("button", { name: /새로고침/ }));

    expect(await screen.findByRole("button", { name: "Bank Survives Collection Failure 상세 열기" })).toBeTruthy();
    expect(screen.getAllByText("Core reading").length).toBeGreaterThan(0);
    expect(screen.getByRole("alert").textContent).toContain("collection_store_unavailable");
    expect(screen.queryByText("Saved papers 컬렉션은 존재하며 저장된 논문이 0개입니다.")).toBeNull();
  });

  it("does not let an older collection request overwrite the newest route result", async () => {
    const user = userEvent.setup();
    renderWorkspace("/topics/topic-a/papers?collection=saved");
    await screen.findByRole("button", { name: "Canonical Systems Paper 상세 열기" });

    const staleNamedRequest = deferred<PaperBankResponse>();
    const baselineCalls = apiMocks.getTopicPaperBank.mock.calls.length;
    apiMocks.getTopicPaperBank.mockImplementationOnce(() => staleNamedRequest.promise);

    const filter = screen.getByRole("combobox", { name: "표시할 컬렉션" });
    await user.selectOptions(filter, "collection-core");
    await waitFor(() => expect(apiMocks.getTopicPaperBank).toHaveBeenCalledTimes(baselineCalls + 1));

    apiMocks.getTopicPaperBank.mockResolvedValueOnce(bank("Newest Saved Result", "saved-papers"));
    await user.selectOptions(filter, "saved-papers");
    expect(await screen.findByRole("button", { name: "Newest Saved Result 상세 열기" })).toBeTruthy();

    await act(async () => {
      staleNamedRequest.resolve(bank("Stale Named Result", "collection-core"));
      await staleNamedRequest.promise;
    });

    expect(screen.getByRole("button", { name: "Newest Saved Result 상세 열기" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stale Named Result 상세 열기" })).toBeNull();
    expect(screen.getByLabelText("현재 경로").textContent).toContain("collection=saved");
  });
});
