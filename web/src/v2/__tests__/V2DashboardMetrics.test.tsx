import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiTopicDashboardResponse } from "../../lib/api";
import { V2DashboardView } from "../views/V2DashboardView";
import { V2Kpi } from "../V2Primitives";

// Icons are rendered by the real-browser test; they do not affect metric text.
vi.mock("@phosphor-icons/react", () => {
  const Icon = () => null;
  return { ArrowRight: Icon, Bell: Icon, BookOpenText: Icon, CalendarBlank: Icon,
    ChartLineUp: Icon, DownloadSimple: Icon, FileText: Icon, Lightning: Icon,
    MagnifyingGlass: Icon, Quotes: Icon, Sparkle: Icon, Star: Icon, SpinnerGap: Icon };
});

// Chart layout is outside jsdom; expose the actual data handed to each chart.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: any) => <div>{children}</div>,
  LineChart: ({ data }: any) => <pre data-testid="line-data">{JSON.stringify(data)}</pre>,
  Line: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null,
  CartesianGrid: () => null, BarChart: () => null, Bar: () => null,
  PieChart: () => null, Pie: () => null, Cell: () => null,
}));

function fixture(labels = ["2024-02", "2026-09", "not-a-d"]): ApiTopicDashboardResponse {
  return {
    metrics: {
      collectedPapers: { value: 9, delta: -2, sparkline: [4, 2, 3] },
      corePapers: { value: 2, delta: 1, sparkline: [1, 0, 1] },
      averageImpact: { value: 42, delta: -3, sparkline: [50, 40, 36] },
      totalCitations: { value: 0, delta: 0, sparkline: [0, 0, 0] },
      trendIndex: { label: "not-a-d", deltaPercent: 50, sparkline: [4, 2, 3] },
    },
    trends: { labels, totalPapers: [4, 2, 3], corePapers: [1, 0, 1], citations: [0, 0, 0] },
    analytics: { artifactVersion: "topic-analytics-v2", coverage: [
      { coverageId: "citation_metadata", covered: 0, total: 9 },
    ] },
    hotKeywords: [], scheduledTasks: [], latestCorePapers: [], fieldDistribution: [],
    institutionDistribution: [], recentReports: [], reviewCoverage: { totalPapers: 9 },
  } as unknown as ApiTopicDashboardResponse;
}

function show(dashboard: ApiTopicDashboardResponse, onRetrySchedules = vi.fn()) {
  return render(<V2DashboardView dashboard={dashboard} busyAction={null} busyTaskId={null}
    onRunAction={vi.fn()} onToggleTask={vi.fn()} onRunTask={vi.fn()}
    onOpenReports={vi.fn()} onOpenPapers={vi.fn()} onOpenSettings={vi.fn()}
    onRetrySchedules={onRetrySchedules} />);
}

function card(label: string) {
  return within(screen.getByText(label).closest("article")!);
}

describe("dashboard metric meaning", () => {
  it("shows schedule read failure and supports keyboard retry instead of a false empty list", async () => {
    const data = fixture([]);
    data.scheduleStatus = { state: "unavailable", warnings: ["예약 작업 저장소가 잠겨 있습니다."] };
    const retry = vi.fn();
    show(data, retry);
    expect(screen.queryByText("예약 작업이 없습니다.")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("예약 작업 저장소가 잠겨 있습니다.");
    const button = screen.getByRole("button", { name: "예약 목록 다시 불러오기" });
    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("labels fresh disabled defaults and keeps a genuinely empty persisted list distinct", () => {
    const data = fixture([]);
    data.scheduleStatus = { state: "defaults", warnings: [] };
    const shown = show(data);
    expect(screen.getByText(/기본 예약 작업.*자동 실행/)).toBeTruthy();
    shown.unmount();
    data.scheduleStatus = { state: "ready", warnings: [] };
    show(data);
    expect(screen.getByText("예약 작업이 없습니다.")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows local counts and valid recorded months without inventing weekly growth", () => {
    show(fixture());
    expect(card("수집된 논문").getByText("9")).toBeTruthy();
    expect(card("평균 선별 점수").getByText("42.0")).toBeTruthy();
    expect(card("최근 집계 월").getByText("2026-09")).toBeTruthy();
    expect(card("최근 집계 월").getByText("해당 월 논문 2편")).toBeTruthy();
    expect(screen.queryByText(/지난 7일|트렌드 지수/)).toBeNull();
    expect(screen.getByText(/유효한 월이 없는 1개 구간/)).toBeTruthy();
    const chartData = JSON.parse(screen.getByTestId("line-data").textContent!);
    expect(chartData.map((point: any) => point.label)).toEqual(["2024-02", "2026-09"]);
    expect(card("인용 수 (확인된 합계)").getByText("미확인")).toBeTruthy();
  });

  it("keeps an unknown or invalid-only month unavailable rather than displaying zero change", () => {
    show(fixture(["unknown", "2026-13", "0000-01"]));
    expect(card("최근 집계 월").getByText("데이터 부족")).toBeTruthy();
    expect(screen.getByText("월별 분포를 표시할 수 없습니다.")).toBeTruthy();
    expect(screen.queryByText(/0%|지난 7일/)).toBeNull();
    expect(screen.queryByTestId("line-data")).toBeNull();
  });

  it("distinguishes known zero citations from absent metadata and identifies partial coverage", () => {
    const data = fixture(["2026-09"]);
    data.analytics!.artifactVersion = "topic-analytics-v3";
    data.analytics!.coverage[0].covered = 1;
    show(data);
    expect(card("인용 수 (확인된 합계)").getByText("0")).toBeTruthy();
    expect(card("인용 수 (확인된 합계)").getByText("인용 메타데이터 1/9편")).toBeTruthy();
  });

  it("does not invent a sparkline when values are absent", () => {
    const { rerender } = render(<V2Kpi label="empty" value="데이터 부족" values={[]} />);
    expect(screen.queryByTestId("line-data")).toBeNull();
    rerender(<V2Kpi label="measured" value="2" values={[1, 2]} />);
    expect(screen.getByTestId("line-data").textContent).toContain('"point":2');
  });

  it("keeps an empty corpus count at zero while its average and citations are unavailable", () => {
    const data = fixture([]);
    data.metrics.collectedPapers.value = 0;
    data.metrics.averageImpact.value = 0;
    data.analytics!.coverage[0].total = 0;
    show(data);
    expect(card("수집된 논문").getByText("0")).toBeTruthy();
    expect(card("평균 선별 점수").getByText("데이터 부족")).toBeTruthy();
    expect(card("인용 수 (확인된 합계)").getByText("미확인")).toBeTruthy();
    expect(screen.queryByTestId("line-data")).toBeNull();
  });

  it("distinguishes a nonempty unscored corpus from explicitly scored zero papers", () => {
    const data = fixture(["2026-09"]);
    data.metrics.averageImpact.value = 0;
    data.analytics!.artifactVersion = "topic-analytics-v3";
    data.analytics!.coverage.push({ coverageId: "triage_score", label: "선별 점수", covered: 0, total: 9, percentage: 0 });
    const { unmount } = show(data);
    expect(card("평균 선별 점수").getByText("데이터 부족")).toBeTruthy();
    unmount();
    data.analytics!.coverage[1].covered = 1;
    show(data);
    expect(card("평균 선별 점수").getByText("0.0")).toBeTruthy();
    expect(card("평균 선별 점수").getByText("선별 점수 1/9편")).toBeTruthy();
  });
});
