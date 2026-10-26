import {
  ArrowRight,
  Bell,
  BookOpenText,
  CalendarBlank,
  ChartLineUp,
  DownloadSimple,
  FileText,
  Lightning,
  MagnifyingGlass,
  Quotes,
  Sparkle,
  Star,
} from "@phosphor-icons/react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ApiScheduledTask, ApiTopicDashboardResponse, TopicAction } from "../../lib/api";
import { V2Button, V2Empty, V2Kpi, V2Panel, V2Toggle, V2_CHART_COLORS, V2_TOOLTIP_STYLE } from "../V2Primitives";

function formatDate(value?: string | null) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "날짜 미확인";
  return parsed.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" });
}

export function V2DashboardView({
  dashboard,
  busyAction,
  busyTaskId,
  onRunAction,
  onToggleTask,
  onRunTask,
  onOpenReports,
  onOpenPapers,
  onOpenSettings,
  onRetrySchedules,
}: {
  dashboard: ApiTopicDashboardResponse;
  busyAction: TopicAction | null;
  busyTaskId: string | null;
  onRunAction: (action: Exclude<TopicAction, "full_pipeline">) => void;
  onToggleTask: (task: ApiScheduledTask, enabled: boolean) => void;
  onRunTask: (task: ApiScheduledTask) => void;
  onOpenReports: () => void;
  onOpenPapers: () => void;
  onOpenSettings: () => void;
  onRetrySchedules?: () => void;
}) {
  const metric = dashboard.metrics;
  // Older saved artifacts may contain unchecked date prefixes. Keep their
  // totals intact, but never present those prefixes as a chronological series.
  const trendData = dashboard.trends.labels.map((label, index) => ({
    label,
    total: dashboard.trends.totalPapers[index] ?? null,
    core: dashboard.trends.corePapers[index] ?? null,
  })).filter(({ label }) => /^(?!0000)\d{4}-(0[1-9]|1[0-2])$/.test(label))
    .sort((left, right) => left.label.localeCompare(right.label));
  const invalidMonths = dashboard.trends.labels.length - trendData.length;
  const latestMonth = trendData.at(-1);
  const scoreCoverage = dashboard.analytics?.coverage.find((item) => item.coverageId === "triage_score");
  const scoreKnown = scoreCoverage ? scoreCoverage.covered > 0 : metric.collectedPapers.value > 0 && metric.averageImpact.value !== 0;
  const scoreBasis = scoreCoverage
    ? `선별 점수 ${scoreCoverage.covered}/${scoreCoverage.total}편`
    : "기존 집계 · 점수 확인 범위 미기록";
  const citationCoverage = dashboard.analytics?.coverage.find((item) => item.coverageId === "citation_metadata");
  const citationsKnown = (citationCoverage?.covered ?? 0) > 0 || metric.totalCitations.value > 0;
  const citationBasis = citationCoverage && citationsKnown
    ? `${dashboard.analytics?.artifactVersion === "topic-analytics-v3" ? "인용 메타데이터" : "기존 집계: 양수 인용"} ${citationCoverage.covered}/${citationCoverage.total}편`
    : "인용 메타데이터 확인 필요";
  const fieldData = dashboard.fieldDistribution.slice(0, 6).map((item) => ({ name: item.label, value: item.count }));
  const institutionData = dashboard.institutionDistribution.slice(0, 5).map((item) => ({ name: item.name, count: item.count }));

  return (
    <div className="v2-screen-stack">
      <section className="v2-kpi-row" aria-label="연구 지표">
        <V2Kpi label="수집된 논문" value={metric.collectedPapers.value.toLocaleString()} delta="현재 집계 · 중복 제거" icon={<BookOpenText size={16} />} />
        <V2Kpi label="핵심 논문" value={metric.corePapers.value.toLocaleString()} delta="로컬 선별·관련성 기준" tone="purple" icon={<FileText size={16} />} />
        <V2Kpi label="평균 선별 점수" value={scoreKnown ? metric.averageImpact.value.toFixed(1) : "데이터 부족"} delta={scoreBasis} tone="green" icon={<Sparkle size={16} />} />
        <V2Kpi label="인용 수 (확인된 합계)" value={citationsKnown ? metric.totalCitations.value.toLocaleString() : "미확인"} delta={citationBasis} tone="amber" icon={<Star size={16} />} />
        <V2Kpi label="최근 집계 월" value={latestMonth?.label ?? "데이터 부족"} delta={latestMonth?.total != null ? `해당 월 논문 ${latestMonth.total.toLocaleString()}편` : "유효한 월별 논문 수 없음"} tone="cyan" icon={<ChartLineUp size={16} />} />
      </section>

      <section className="v2-dashboard-grid">
        <V2Panel title="수집 논문의 월별 분포" subtitle="최대 12개 기록 월 · 발행·갱신월 우선, 날짜 미확인 시 실행월 포함" className="v2-trend-panel">
          <p className="v2-distribution-note">수집한 논문의 날짜 분포입니다. 시점 간 변화는 인사이트의 스냅샷 비교에서 확인하세요.</p>
          {invalidMonths > 0 ? <p className="v2-distribution-note">유효한 월이 없는 {invalidMonths}개 구간은 그래프에서 제외했습니다. 논문 총계는 유지됩니다.</p> : null}
          {trendData.length ? (
            <div className="v2-chart-large">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trendData} margin={{ top: 12, right: 14, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="var(--border-default)" vertical={false} />
                  <XAxis dataKey="label" stroke="var(--text-muted)" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis stroke="var(--text-muted)" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={V2_TOOLTIP_STYLE} />
                  <Line type="monotone" dataKey="total" name="전체 논문" stroke={V2_CHART_COLORS[0]} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
                  <Line type="monotone" dataKey="core" name="핵심 논문" stroke={V2_CHART_COLORS[1]} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : <V2Empty title="월별 분포를 표시할 수 없습니다." description="유효한 발행일·갱신일 또는 실행일이 있는 논문 데이터가 필요합니다." />}
        </V2Panel>

        <V2Panel title="핫 토픽 키워드" subtitle="현재 API 응답의 점수·논문 수">
          <ol className="v2-ranked-bars">
            {dashboard.hotKeywords.slice(0, 5).map((keyword, index) => (
              <li key={keyword.keyword}>
                <span>{index + 1}</span><strong>{keyword.keyword}</strong>
                <i style={{ width: `${Math.max(20, Math.min(100, keyword.score))}%` }} />
                <em>{keyword.paperCount ?? Math.round(keyword.score)}</em>
              </li>
            ))}
          </ol>
        </V2Panel>

        <V2Panel title="예약 작업 (Scheduled Tasks)" subtitle="기존 작업의 실행·활성화만 지원">
          {dashboard.scheduleStatus?.state === "unavailable" ? (
            <div className="v2-state-card is-error" role="alert">
              <strong>예약 목록을 불러오지 못했습니다.</strong>
              <p>{dashboard.scheduleStatus.warnings.join(" ") || "잠시 후 목록을 다시 불러오세요."}</p>
              {onRetrySchedules ? <V2Button onClick={onRetrySchedules}>예약 목록 다시 불러오기</V2Button> : null}
            </div>
          ) : <>
          {dashboard.scheduleStatus?.state === "defaults" ? (
            <p className="v2-distribution-note">새 저장소의 기본 예약 작업입니다. 자동 실행은 꺼져 있습니다.</p>
          ) : null}
          <div className="v2-schedule-list">
            {dashboard.scheduledTasks.slice(0, 5).map((task, index) => (
              <article key={task.id}>
                <span>{index + 1}</span>
                <button type="button" onClick={() => onRunTask(task)} disabled={busyTaskId === task.id}>
                  <strong>{task.title}</strong><small>{task.scheduleLabel}</small>
                </button>
                <V2Toggle checked={task.enabled} disabled={busyTaskId === task.id} label={`${task.title} 활성화`} onChange={(enabled) => onToggleTask(task, enabled)} />
              </article>
            ))}
            {!dashboard.scheduledTasks.length ? <V2Empty title="예약 작업이 없습니다." description="이 주제에 표시할 예약 작업이 없습니다." /> : null}
          </div>
          </>}
        </V2Panel>

        <V2Panel title="최신 핵심 논문" action={<button type="button" className="v2-text-link" onClick={onOpenPapers}>모든 논문 보기 <ArrowRight size={13} /></button>} className="v2-latest-papers-panel">
          <div className="v2-compact-paper-table">
            <div className="v2-table-head"><span>논문 제목</span><span>저자</span><span>발표일</span><span>인용 수</span><span>선별 점수</span></div>
            {dashboard.latestCorePapers.slice(0, 5).map((paper) => (
              <article key={paper.id}>
                <span className="v2-paper-title"><FileText size={16} /><strong>{paper.title}</strong></span>
                <span>{paper.authors[0] ?? paper.sourceProvider ?? "-"}</span>
                <span>{formatDate(paper.publishedAt)}</span>
                <span>{paper.citations > 0 ? paper.citations : "미확인"}</span>
                <span>{paper.impactScore > 0 ? paper.impactScore.toFixed(1) : "미확인"}</span>
              </article>
            ))}
            {!dashboard.latestCorePapers.length ? <V2Empty title="핵심 논문이 없습니다." description="논문 수집과 리뷰가 완료되면 여기에 표시됩니다." /> : null}
          </div>
        </V2Panel>

        <div className="v2-dashboard-mid-stack">
          <V2Panel title="연구 분야 분포">
            <div className="v2-donut-layout">
              <div className="v2-donut-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart><Pie data={fieldData} dataKey="value" nameKey="name" innerRadius="56%" outerRadius="86%" paddingAngle={1} isAnimationActive={false}>{fieldData.map((entry, index) => <Cell key={entry.name} fill={V2_CHART_COLORS[index % V2_CHART_COLORS.length]} />)}</Pie><Tooltip contentStyle={V2_TOOLTIP_STYLE} /></PieChart>
                </ResponsiveContainer>
                <strong>{dashboard.reviewCoverage.totalPapers.toLocaleString()}<small>Total</small></strong>
              </div>
              <ul>{fieldData.map((item, index) => <li key={item.name}><i style={{ backgroundColor: V2_CHART_COLORS[index % V2_CHART_COLORS.length] }} /><span>{item.name}</span><strong>{item.value}</strong></li>)}</ul>
            </div>
          </V2Panel>
          <V2Panel title="연구 기관 분포 (Top 5)">
            <div className="v2-chart-compact">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={institutionData} layout="vertical" margin={{ left: 12, right: 8 }}>
                  <XAxis type="number" hide />
                  <YAxis dataKey="name" type="category" width={122} tick={{ fill: "var(--text-secondary)", fontSize: 9 }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={V2_TOOLTIP_STYLE} />
                  <Bar dataKey="count" fill="var(--chart-series-1)" radius={[3, 3, 3, 3]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </V2Panel>
        </div>

        <div className="v2-dashboard-right-stack">
          <V2Panel title="최근 생성된 보고서">
            <div className="v2-recent-report-list">
              {dashboard.recentReports.slice(0, 4).map((report) => (
                <button type="button" key={report.id} onClick={onOpenReports}>
                  <FileText size={17} weight="duotone" />
                  <span><strong>{report.title}</strong><small>{formatDate(report.createdAt)}</small></span>
                  {report.downloadUrl ? <DownloadSimple size={16} /> : <ArrowRight size={16} />}
                </button>
              ))}
            </div>
            <button type="button" className="v2-text-link" onClick={onOpenReports}>모든 보고서 보기 <ArrowRight size={13} /></button>
          </V2Panel>

          <V2Panel title="빠른 작업">
            <div className="v2-quick-actions">
              <V2Button busy={busyAction === "collect"} onClick={() => onRunAction("collect")}><DownloadSimple size={16} />새 논문 수집</V2Button>
              <V2Button busy={busyAction === "analyze"} onClick={() => onRunAction("analyze")}><Lightning size={16} />즉시 분석 실행</V2Button>
              <V2Button busy={busyAction === "report"} onClick={() => onRunAction("report")}><FileText size={16} />보고서 생성</V2Button>
              <V2Button onClick={onOpenSettings}><Bell size={16} />알림 설정</V2Button>
            </div>
          </V2Panel>
        </div>
      </section>
    </div>
  );
}
