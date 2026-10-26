import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getApiEvidenceItem,
  getApiEvidenceSearch,
  type EvidenceSearchResponse,
} from "../api";

const emptySearchResponse: EvidenceSearchResponse = {
  query: {
    raw: "",
    normalized: "",
    tokens: [],
    filters: {
      topicIds: [],
      runIds: [],
      corpusKinds: [],
      reviewSources: [],
      providerIds: [],
      reviewLevels: [],
      hasEvidence: null,
    },
  },
  page: {
    limit: 25,
    returnedCount: 0,
    total: 0,
    totalRelation: "exact",
    nextCursor: null,
  },
  facets: {
    corpusKinds: {},
    reviewLevels: {},
    reviewSources: {},
    providers: {},
    statuses: {},
  },
  items: [],
};

describe("evidence API client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("serializes non-empty search filters as the committed CSV query contract", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => emptySearchResponse,
    });

    await getApiEvidenceSearch(
      {
        q: "spatial evidence",
        topic_ids: ["topic-alpha", "topic-beta"],
        run_ids: [],
        corpus_kinds: ["paper", "evidence"],
        review_sources: ["full_text"],
        provider_ids: ["codex"],
        review_levels: ["full_text_reviewed"],
        has_evidence: false,
        limit: 50,
        cursor: "opaque+/=cursor",
      },
      { signal: controller.signal },
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsed = new URL(url);
    expect(parsed.pathname).toBe("/api/evidence/search");
    expect(Object.fromEntries(parsed.searchParams)).toEqual({
      q: "spatial evidence",
      topic_ids: "topic-alpha,topic-beta",
      corpus_kinds: "paper,evidence",
      review_sources: "full_text",
      provider_ids: "codex",
      review_levels: "full_text_reviewed",
      has_evidence: "false",
      limit: "50",
      cursor: "opaque+/=cursor",
    });
    expect(parsed.searchParams.has("run_ids")).toBe(false);
    expect(init.signal).toBe(controller.signal);
    expect(init.method).toBeUndefined();
  });

  it("encodes the detail result path and optional occurrence context", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ resultId: "paper:alpha/beta" }) });

    await getApiEvidenceItem("paper:alpha/beta", { occurrence_run_id: "run 1" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsed = new URL(url);
    expect(parsed.pathname).toBe("/api/evidence/items/paper%3Aalpha%2Fbeta");
    expect(parsed.searchParams.get("occurrence_run_id")).toBe("run 1");
    expect(init.method).toBeUndefined();
  });

  it("preserves retryable 503 status and body for truthful UI handling", async () => {
    const body = JSON.stringify({
      detail: {
        code: "search_too_expensive",
        message: "The evidence search exceeded its local read budget.",
        retryable: true,
      },
    });
    fetchMock.mockResolvedValue({ ok: false, status: 503, text: async () => body });

    await expect(getApiEvidenceSearch({ q: "broad" })).rejects.toMatchObject({
      status: 503,
      body,
    });
  });
});
