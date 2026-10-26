import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { ApiReportsResponse, ApiTopicDashboardResponse, ApiTopicPapersResponse, TopicProfile } from "../../lib/api";
import { V2PapersView } from "../views/V2PapersView";
import { V2ReportsView } from "../views/V2ReportsView";
import { V2SettingsView } from "../views/V2SettingsView";
import { AppearanceProvider } from "../AppearanceProvider";

vi.mock("../../lib/modelConnections", () => ({ getModelConnections: vi.fn().mockResolvedValue({ revision: 0, default_connector_id: "codex_subscription", effective_connector_id: "codex_subscription", environment_override: null, execution_allowed: false, connectors: [{ connector_id: "codex_subscription", display_name: "Codex · ChatGPT 구독", auth_mode: "subscription_cli", provider_name: "codex_cli", model: "", adapter_available: true, credential_env: null, credential_present: null, cli_command: "codex login", login_instructions: "공식 CLI에서 로그인합니다.", docs_url: "https://example.invalid/docs", availability_reason: "실행 권한은 별도입니다." }] }), saveModelConnections: vi.fn() }));

const dashboardFixture = {
  topic: { id: "large-language-models", name: "Large Language Models", description: "LLM systems", updatedAt: "2026-08-26T00:00:00Z", isFavorite: true, status: "active" },
  metrics: { corePapers: { value: 4 } },
  hotKeywords: [{ keyword: "retrieval", score: 80 }],
  recentReports: [{ id: "report-1" }],
  reviewCoverage: { totalPapers: 4, reviewedCount: 4, abstractOnlyCount: 0, fullTextReviewedCount: 4 },
  researchOperations: { operationsPlanner: { topicConfiguration: { queryTerms: ["retrieval", "alignment"], minRelevance: 60 } } },
} as unknown as ApiTopicDashboardResponse;

const papersFixture = {
  topic: dashboardFixture.topic,
  items: [
    {
      id: "paper-1",
      runId: "run-1",
      title: "Retrieval-aware language model evaluation",
      authors: ["A. Researcher"],
      publishedAt: "2026-08-20T00:00:00Z",
      citations: 42,
      impactScore: 8.4,
      sourceUrl: "https://example.invalid/paper-1",
      sourceProvider: "arxiv",
      saved: false,
      theme: "Retrieval",
      status: "core",
    },
  ],
  total: 1,
  q: "",
  providerFilter: null,
  providerCounts: [{ provider: "arxiv", label: "arXiv", paperCount: 1 }],
  provenance: null,
  warnings: [],
} as ApiTopicPapersResponse;

const reportsFixture = {
  items: [
    {
      id: "report-1",
      runId: "run-1",
      topicId: "large-language-models",
      topicName: "Large Language Models",
      title: "LLM 주간 트렌드 리포트",
      createdAt: "2026-08-26T00:00:00Z",
      updatedAt: "2026-08-26T00:05:00Z",
      status: "completed",
      downloadUrl: "/reports/report-1/download",
      error: null,
      sourceProviders: ["arxiv"],
      completedVersionCount: 1,
      latestCompletedVersionId: "version-1",
      latestCompletedVersionAt: "2026-08-26T00:05:00Z",
      latestCompletedVersionHash: "sha256:test",
      exportArtifacts: [],
      exportArtifactSummary: { total: 1, downloadable: 1, unavailable: 0, formats: ["report_markdown"] },
    },
  ],
  total: 1,
  provenance: null,
  warnings: [],
} as ApiReportsResponse;

const profileFixture = {
  profile_id: "large-language-models",
  title: "Large Language Models",
  description: "LLM systems",
  topic: "Large Language Models",
  queries: ["retrieval", "alignment"],
  source_settings: { defaultProvider: "arxiv", enabledProviders: ["arxiv"], providers: {} },
  default_collection_cadence: "manual",
  default_report_cadence: "weekly",
} as TopicProfile;

describe("v2 workspace views", () => {
  it("keeps real paper search functional and blocks unsupported selected-corpus report generation", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();

    render(
      <MemoryRouter><V2PapersView
        papers={papersFixture}
        dashboard={dashboardFixture}
        query=""
        provider=""
        onSearch={onSearch}
        onProviderChange={vi.fn()}
      /></MemoryRouter>,
    );

    const search = screen.getByRole("textbox", { name: "논문 제목, 저자, 키워드 검색" });
    await user.type(search, "retrieval{Enter}");
    expect(onSearch).toHaveBeenCalledWith("retrieval");

    await user.click(screen.getByRole("checkbox", { name: `${papersFixture.items[0].title} 선택` }));
    expect(screen.getByText("1 선택됨")).toBeTruthy();
    const reportButton = screen.getByRole("button", { name: /선택 보고서/ });
    expect((reportButton as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/선택 상태는 현재 화면에서만 유지/)).toBeTruthy();
  });

  it("renders the report lifecycle in the v2 reports layout", () => {
    render(
      <V2ReportsView
        reports={reportsFixture}
        schedules={[]}
        busyAction={null}
        busyScheduleId={null}
        onGenerateReport={vi.fn()}
        onToggleSchedule={vi.fn()}
        resolveReportUrl={(value) => value ?? null}
      />,
    );

    expect(screen.getByText("최근 생성된 보고서")).toBeTruthy();
    expect(screen.getAllByText("LLM 주간 트렌드 리포트").length).toBeGreaterThan(0);
    expect(screen.getByText("보고서 생성 계약")).toBeTruthy();
    expect(screen.getByText("추천 보고서 작업")).toBeTruthy();
  });

  it("keeps the strict 100 percent weight preset separate from persisted relevance", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(
      <MemoryRouter><AppearanceProvider><V2SettingsView
        topicId="large-language-models"
        dashboard={dashboardFixture}
        profile={profileFixture}
        providers={null}
        notificationSettings={null}
        schedules={[]}
        settingsStatus={null}
        busy={false}
        onSave={onSave}
      /></AppearanceProvider></MemoryRouter>,
    );

    const total = screen.getByText(/참고 프리셋/).parentElement;
    expect(total).not.toBeNull();
    expect(within(total as HTMLElement).getByText("100%")).toBeTruthy();
    await user.selectOptions(screen.getByRole("combobox", { name: "테마" }), "solarized-light");
    await user.selectOptions(screen.getByRole("combobox", { name: "강조색" }), "violet");
    await user.selectOptions(screen.getByRole("combobox", { name: "화면 밀도" }), "compact");
    await user.selectOptions(screen.getByRole("combobox", { name: "동작 설정" }), "reduced");
    expect(document.documentElement.dataset.theme).toBe("solarized-light");
    expect(document.documentElement.dataset.accent).toBe("violet");
    expect(document.documentElement.dataset.density).toBe("compact");
    expect(document.documentElement.dataset.motion).toBe("reduced");
    expect(screen.getByText("팀 협업 및 RBAC는 제공되지 않습니다.")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "제품 세션 현재 상태" })).toBeTruthy();
    expect(screen.queryByText("선택 Provider")).toBeNull();
    const connections = screen.getByRole("region", { name: "모델 연결 설정" });
    expect(await within(connections).findByRole("combobox", { name: "기본 모델 연결" })).toHaveProperty("value", "codex_subscription");

    const title = screen.getByLabelText("주제 이름");
    await user.clear(title);
    await user.type(title, "LLM Systems Review");
    await user.click(screen.getByRole("button", { name: "지원 설정 저장" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0]).toMatchObject({
      title: "LLM Systems Review",
      minRelevance: dashboardFixture.researchOperations.operationsPlanner.topicConfiguration.minRelevance,
    });
  });
});
