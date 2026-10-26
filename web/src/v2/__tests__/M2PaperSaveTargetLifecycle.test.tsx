import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PaperBankCollectionList, PaperBankResponse } from "../paperBankApi";

const apiMocks = vi.hoisted(() => ({
  addPaperToCollection: vi.fn(),
  getApiTopicDashboard: vi.fn(),
  getPaperCollections: vi.fn(),
  getTopicPaperBank: vi.fn(),
  getTopicProfiles: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    getApiTopicDashboard: apiMocks.getApiTopicDashboard,
    getTopicProfiles: apiMocks.getTopicProfiles,
  };
});

vi.mock("../paperBankApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../paperBankApi")>();
  return {
    ...actual,
    addPaperToCollection: apiMocks.addPaperToCollection,
    getPaperCollections: apiMocks.getPaperCollections,
    getTopicPaperBank: apiMocks.getTopicPaperBank,
  };
});

import { V2TopicWorkspaceScreen } from "../V2TopicWorkspaceScreen";

const paperBank = {
  topicId: "topic-a",
  items: [{
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
    sourceUrl: null,
    sourceUrlState: "unavailable",
    theme: "Systems",
    review: { state: "not_reviewed", updatedAt: null, evidenceCount: 0 },
    collectionIds: [],
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
    collectionId: null,
    reviewState: null,
    sort: "published_desc",
  },
  queryDigest: "b".repeat(64),
  providerCounts: [{ provider: "arxiv", label: "arXiv", paperCount: 1 }],
  reviewResource: { state: "available", reason: null },
  reviewSummary: { notReviewed: 1, abstractReviewed: 0, fullTextAvailable: 0, fullTextReviewed: 0 },
  collectionResource: { state: "available", reason: null },
  resource: { state: "available", reason: null },
  unsupportedFilters: [],
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

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.getApiTopicDashboard.mockResolvedValue({
    topic: { id: "topic-a", name: "Topic A", updatedAt: null },
  });
  apiMocks.getTopicProfiles.mockResolvedValue({ items: [] });
  apiMocks.getPaperCollections.mockResolvedValue(collections);
  apiMocks.getTopicPaperBank.mockResolvedValue(paperBank);
  apiMocks.addPaperToCollection.mockResolvedValue(collections.items[1]);
});

describe("M2 named save-target lifecycle", () => {
  it("retains the named write target when the collection read filter is cleared", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/topics/topic-a/papers?collection=collection-core"]}>
        <Routes>
          <Route path="/topics/:topicId/:tab" element={<V2TopicWorkspaceScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    const target = await screen.findByRole("combobox", { name: "저장 대상 컬렉션" });
    await user.selectOptions(target, "collection-core");
    expect((target as HTMLSelectElement).value).toBe("collection-core");

    await user.selectOptions(screen.getByRole("combobox", { name: "표시할 컬렉션" }), "");
    await waitFor(() => expect(apiMocks.getTopicPaperBank).toHaveBeenLastCalledWith(
      "topic-a",
      expect.objectContaining({ collectionId: "" }),
    ));

    const retainedTarget = await screen.findByRole("combobox", { name: "저장 대상 컬렉션" });
    expect((retainedTarget as HTMLSelectElement).value).toBe("collection-core");
    await user.click(screen.getByRole("button", { name: "Canonical Systems Paper 저장" }));

    await waitFor(() => expect(apiMocks.addPaperToCollection).toHaveBeenCalledWith(
      "topic-a",
      "collection-core",
      "version-a-v2",
    ));
  });
});
