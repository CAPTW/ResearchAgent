import { useRef, useState } from "react";
import { ReportVersionPanel } from "./ReportVersionPanel";
import {
  ArrowRight,
  CalendarBlank,
  ChartLineUp,
  DownloadSimple,
  FileText,
  Sparkle,
  Star,
} from "@phosphor-icons/react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { ApiReportHubItem, ApiReportsResponse, TopicAction, TopicSchedule } from "../../lib/api";
import { reportDownloadableArtifactCount } from "../../lib/reportArtifacts";
import { V2Button, V2Empty, V2Kpi, V2Panel, V2Toggle, V2_CHART_COLORS, V2_TOOLTIP_STYLE } from "../V2Primitives";
import { formatTimestamp } from "../appearance";
import { useOptionalAppearance } from "../AppearanceProvider";

function formatDate(value?: string | null, timeFormat: "locale" | "12h" | "24h" = "locale") {
  if (!value) return "-";
  return formatTimestamp(value, timeFormat, "ko-KR");
}

export function V2ReportsView({
  reports,
  schedules,
  busyAction,
  busyScheduleId,
  onGenerateReport,
  onToggleSchedule,
  resolveReportUrl,
}: {
  reports: ApiReportsResponse;
  schedules: TopicSchedule[];
  busyAction: TopicAction | null;
  busyScheduleId: string | null;
  onGenerateReport: () => void;
  onToggleSchedule: (schedule: TopicSchedule, enabled: boolean) => void;
  resolveReportUrl: (url?: string | null) => string | null;
}) {
  const { preferences } = useOptionalAppearance();
  const [selectedReport,setSelectedReport]=useState<ApiReportHubItem|null>(null);
  const lastOpened=useRef<HTMLButtonElement|null>(null);
  const completed = reports.items.filter((item) => item.status === "completed");
  const downloadable = reports.items.reduce((total, report) => total + reportDownloadableArtifactCount(report), 0);
  const versionCount = reports.items.reduce((total, report) => total + report.completedVersionCount, 0);
  const performance = [...reports.items].reverse().slice(-8).map((report) => ({
    label: formatDate(report.createdAt, preferences.timeFormat ?? "locale").split(" ")[0],
    reports: 1,
    versions: report.completedVersionCount,
    downloads: reportDownloadableArtifactCount(report),
  }));

  return (
    <div className="v2-screen-stack">
      <section className="v2-kpi-row">
        <V2Kpi label="전체 생성된 보고서" value={reports.total} delta={`+${reports.items.length} 현재 조회`} values={reports.items.map((item) => item.completedVersionCount + 1)} icon={<FileText size={16} />} />
        <V2Kpi label="예약 발행 보고서" value={schedules.filter((item) => item.enabled).length} delta={`${schedules.length}개 일정`} values={schedules.map((item, index) => item.enabled ? index + 2 : 1)} tone="purple" icon={<CalendarBlank size={16} />} />
        <V2Kpi label="다운로드 가능 artifact" value={downloadable.toLocaleString()} delta={`${completed.length}개 완료 보고서`} values={reports.items.map((item) => reportDownloadableArtifactCount(item))} tone="green" icon={<DownloadSimple size={16} />} />
        <V2Kpi label="완료 버전" value={versionCount} delta={`${completed.length}/${reports.total} 완료`} values={reports.items.map((item) => item.completedVersionCount)} tone="amber" icon={<Star size={16} />} />
        <V2Kpi label="조회된 보고서 완료 비율" value={reports.items.length ? `${Math.round((completed.length / reports.items.length) * 100)}%` : "자료 없음"} delta="현재 조회 레코드 기준" values={performance.map((item) => item.reports)} tone="cyan" icon={<ChartLineUp size={16} />} />
      </section>

      <section className="v2-reports-grid">
        <V2Panel title="최근 생성된 보고서" className="v2-report-table-panel">
          <div className="v2-report-table">
            <div className="v2-report-table-head"><span>보고서 제목</span><span>생성일</span><span>상태</span><span>대상</span><span>완료 버전</span><span>작업</span></div>
            {reports.items.map((report) => {
              const downloadUrl = resolveReportUrl(report.downloadUrl);
              return (
                <article key={report.id}>
                  <span className="v2-report-title"><FileText size={16} weight="duotone" /><span><strong>{report.title}</strong><small>{report.sourceProviders.join(" · ") || "Provider 출처 미확인"}</small></span></span>
                  <span>{formatDate(report.createdAt, preferences.timeFormat ?? "locale")}</span>
                  <span><em className={`v2-status is-${report.status}`}>{report.status === "completed" ? "완료" : report.status}</em></span>
                  <span>{report.topicName}</span>
                  <span>{report.completedVersionCount}</span>
                  <span className="v2-row-actions"><button type="button" aria-label={`${report.title} 저장 버전 열기`} onClick={event=>{lastOpened.current=event.currentTarget;setSelectedReport(report);}}>버전 열기</button>{downloadUrl ? <a href={downloadUrl} aria-label={`${report.title} 다운로드`}><DownloadSimple size={15} /></a> : <em>artifact 없음</em>}</span>
                </article>
              );
            })}
            {!reports.items.length ? <V2Empty title="생성된 보고서가 없습니다." description="첫 보고서를 생성해 연구 결과를 고정하세요." /> : null}
          </div>
          <p className="v2-centered-link">현재 API 응답 {reports.items.length}개 표시</p>
          {reports.total>reports.items.length?<p>전체 {reports.total}개 중 현재 조회한 {reports.items.length}개입니다.</p>:null}
        </V2Panel>

        <V2Panel title="보고서 생성 계약">
          <div className="v2-template-list">
            <button type="button" onClick={onGenerateReport}><span className="tone-1"><FileText size={18} /></span><span><strong>Weekly Journal 보고서</strong><small>현재 제품의 검증된 보고서 계약</small></span><em>생성</em></button>
            <div className="v2-deferred-item"><span className="tone-2"><Sparkle size={18} /></span><span><strong>추가 템플릿 · M4</strong><small>템플릿별 별도 corpus/output 계약이 아직 없습니다.</small></span></div>
          </div>
        </V2Panel>

        <V2Panel title="예약 발행 (자동 보고서 일정)">
          <div className="v2-schedule-table">
            <div><span>보고서 이름</span><span>주기</span><span>다음 발행일</span><span>상태</span></div>
            {schedules.slice(0, 5).map((schedule) => (
              <article key={schedule.schedule_id}><strong>{schedule.title}</strong><span>{schedule.cadence_label}</span><span>{formatDate(schedule.next_run_at, preferences.timeFormat ?? "locale")}</span><V2Toggle checked={schedule.enabled} disabled={busyScheduleId === schedule.schedule_id} label={`${schedule.title} 일정`} onChange={(enabled) => onToggleSchedule(schedule, enabled)} /></article>
            ))}
            {!schedules.length ? <V2Empty title="등록된 일정이 없습니다." description="M1에서는 기존 일정 토글만 지원하며 새 일정 생성은 제공하지 않습니다." /> : null}
          </div>
        </V2Panel>

        <V2Panel title="보고서·완료 버전·다운로드 artifact 기록" subtitle="각 점은 현재 API가 반환한 실제 보고서 레코드입니다." className="v2-report-performance">
          <div className="v2-chart-medium">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={performance} margin={{ top: 8, right: 8, bottom: 0, left: -24 }}>
                <CartesianGrid stroke="var(--border-default)" vertical={false} />
                <XAxis dataKey="label" stroke="var(--text-muted)" tick={{ fontSize: 9 }} axisLine={false} tickLine={false} />
                <YAxis stroke="var(--text-muted)" tick={{ fontSize: 9 }} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={V2_TOOLTIP_STYLE} />
                <Line dataKey="reports" name="보고서" stroke={V2_CHART_COLORS[0]} strokeWidth={2} isAnimationActive={false} />
                <Line dataKey="versions" name="버전" stroke={V2_CHART_COLORS[1]} strokeWidth={2} isAnimationActive={false} />
                <Line dataKey="downloads" name="다운로드" stroke={V2_CHART_COLORS[2]} strokeWidth={2} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </V2Panel>

        <V2Panel title="추천 보고서 작업">
          <div className="v2-report-actions-grid">
            <V2Button className="v2-action-card" busy={busyAction === "report"} onClick={onGenerateReport}><FileText size={19} /><span><strong>Weekly Journal 생성</strong><small>현재 검증된 보고서 생성 경로</small></span><ArrowRight size={15} /></V2Button>
            <V2Button className="v2-action-card" disabled title="별도 경영진 요약 output 계약은 M4 범위입니다."><Sparkle size={19} /><span><strong>경영진 요약 · M4</strong><small>현재 사용할 수 없음</small></span></V2Button>
          </div>
        </V2Panel>

        <V2Panel title="완료 버전이 많은 보고서" subtitle="인기도가 아니라 현재 저장된 완료 버전 수 기준입니다.">
          <ol className="v2-popular-reports">{[...reports.items].sort((a, b) => b.completedVersionCount - a.completedVersionCount).slice(0, 5).map((report, index) => <li key={report.id}><span>{index + 1}</span><strong>{report.title}</strong><em>{report.completedVersionCount} 완료 버전</em></li>)}</ol>
        </V2Panel>
      </section>
      {selectedReport?<ReportVersionPanel key={selectedReport.runId} report={selectedReport} onClose={()=>{setSelectedReport(null);lastOpened.current?.focus();}}/>:null}
    </div>
  );
}
