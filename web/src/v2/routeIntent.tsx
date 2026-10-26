import { ArrowLeft, Info } from "@phosphor-icons/react";
import { Link, useLocation } from "react-router-dom";
import { V2SearchHelpView } from "./views/V2SearchHelpView";

export const V2_TOPIC_TABS = [
  { id: "dashboard", label: "대시보드" },
  { id: "papers", label: "논문 목록" },
  { id: "insights", label: "분석 인사이트" },
  { id: "reports", label: "보고서" },
  { id: "settings", label: "설정" },
] as const;

export type V2TopicTab = (typeof V2_TOPIC_TABS)[number]["id"];
export type CompatibilityMilestone = "M1" | "M2" | "M3" | "M4" | "M5";

export type RouteIntent = {
  capability: string;
  identifiers: Record<string, string>;
  parameters: Record<string, string>;
  closestTab: V2TopicTab;
  targetMilestone: CompatibilityMilestone;
  reason: string;
  adminBoundary: boolean;
};

const RELEVANT_PARAMETERS = ["view", "action", "section", "collection", "provider", "query"] as const;

function decode(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function routeDefinition(pathname: string): Omit<RouteIntent, "identifiers" | "parameters"> {
  if (/^\/reports\/[^/]+$/.test(pathname)) {
    return { capability: "보고서 상세·버전 작업공간", closestTab: "reports", targetMilestone: "M4", reason: "M1은 보고서 허브와 실제 다운로드만 유지합니다.", adminBoundary: false };
  }
  if (pathname === "/reports") {
    return { capability: "보고서 허브", closestTab: "reports", targetMilestone: "M1", reason: "현재 보고서 허브로 연결할 수 있습니다.", adminBoundary: false };
  }
  if (pathname.includes("/runs/") || pathname === "/runs" || pathname === "/new-run") {
    return { capability: "실행 상세·실행 생성 작업공간", closestTab: "dashboard", targetMilestone: "M2", reason: "M1은 실행 식별자를 보존하고 현재 실행 액션만 유지합니다.", adminBoundary: false };
  }
  if (pathname === "/provenance" || pathname === "/evidence") {
    return { capability: "증거·provenance 작업공간", closestTab: "papers", targetMilestone: "M2", reason: "M1은 증거 계약을 보존하지만 전용 작업공간을 확장하지 않습니다.", adminBoundary: false };
  }
  if (pathname === "/recommendations" || pathname === "/compare") {
    return { capability: "추천·비교 작업공간", closestTab: "insights", targetMilestone: "M3", reason: "M1은 현재 파생 지표만 진실하게 표시하고 M3 분석을 구현하지 않습니다.", adminBoundary: false };
  }
  if (pathname === "/automations") {
    return { capability: "자동화 작업공간", closestTab: "reports", targetMilestone: "M5", reason: "M1은 검증된 일정 토글만 유지합니다.", adminBoundary: false };
  }
  if (pathname === "/settings/notifications") {
    return { capability: "알림 설정", closestTab: "settings", targetMilestone: "M5", reason: "M1은 현재 서버가 검증한 알림 설정만 유지합니다.", adminBoundary: false };
  }
  if (pathname === "/settings/analysis") {
    return { capability: "분석 설정 상세", closestTab: "settings", targetMilestone: "M2", reason: "M1은 현재 검증된 주제 설정과 appearance 설정만 제공합니다.", adminBoundary: false };
  }
  if (pathname === "/project-bundle") {
    return { capability: "Project bundle", closestTab: "settings", targetMilestone: "M4", reason: "M1 desktop foundation은 Project Source/package 작업을 수행하지 않습니다.", adminBoundary: false };
  }
  if (pathname === "/admin/legacy-runs") {
    return { capability: "legacy run 운영 도구", closestTab: "settings", targetMilestone: "M5", reason: "관리자 경계를 유지하며 M1 사용자 셸에 운영 도구를 가장하지 않습니다.", adminBoundary: true };
  }
  if (pathname === "/portfolio") {
    return { capability: "연구 포트폴리오", closestTab: "dashboard", targetMilestone: "M2", reason: "M1은 주제 중심 대시보드를 유지하며 포트폴리오 집계를 구현하지 않습니다.", adminBoundary: false };
  }
  return { capability: "호환 경로", closestTab: "dashboard", targetMilestone: "M2", reason: "요청된 경로의 식별자를 보존했지만 M1 범위의 실제 화면은 아닙니다.", adminBoundary: false };
}

export function describeRouteIntent(pathname: string, search: string): RouteIntent {
  const parts = pathname.split("/").filter(Boolean);
  const identifiers: Record<string, string> = {};
  if (parts[0] === "topics" && parts[1]) identifiers.topicId = decode(parts[1]) ?? parts[1];
  const runIndex = parts.indexOf("runs");
  if (runIndex >= 0 && parts[runIndex + 1]) identifiers.runId = decode(parts[runIndex + 1]) ?? parts[runIndex + 1];
  if (parts[0] === "runs" && parts[1]) identifiers.runId = decode(parts[1]) ?? parts[1];
  if (parts[0] === "reports" && parts[1]) identifiers.reportId = decode(parts[1]) ?? parts[1];

  const query = new URLSearchParams(search);
  const parameters: Record<string, string> = {};
  for (const key of RELEVANT_PARAMETERS) {
    const value = query.get(key);
    if (value !== null) parameters[key] = value;
  }
  return { ...routeDefinition(pathname), identifiers, parameters };
}

export function V2CompatibilityRoute() {
  const location = useLocation();
  const intent = describeRouteIntent(location.pathname, location.search);
  const closestPath = intent.identifiers.topicId
    ? `/topics/${encodeURIComponent(intent.identifiers.topicId)}/${intent.closestTab}`
    : "/";
  return (
    <section className="v2-compatibility-state" role="status" aria-label="호환 경로 상태">
      <span className="v2-compatibility-icon"><Info size={24} weight="duotone" /></span>
      <div>
        <p className="v2-eyebrow">Compatibility route · {intent.targetMilestone}</p>
        <h1>{intent.capability}</h1>
        <p>{intent.reason}</p>
        <p><strong>현재 데이터 가용성:</strong> M1 전용 화면/계약 없음. 식별자와 요청 의도만 안전하게 보존했습니다.</p>
        {Object.keys(intent.identifiers).length ? (
          <dl aria-label="보존된 식별자">
            {Object.entries(intent.identifiers).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}
          </dl>
        ) : null}
        {Object.keys(intent.parameters).length ? (
          <ul aria-label="보존된 경로 의도">
            {Object.entries(intent.parameters).map(([key, value]) => <li key={key}>{key}: {value}</li>)}
          </ul>
        ) : null}
        {intent.adminBoundary ? <p className="v2-inline-notice is-warning">관리자 권한 경계를 우회하지 않습니다.</p> : null}
        <Link className="v2-button is-primary" to={closestPath}><ArrowLeft size={15} />가장 가까운 현재 워크스페이스로 이동</Link>
      </div>
    </section>
  );
}

export function V2EvidenceRoute() {
  const location = useLocation();
  return new URLSearchParams(location.search).get("view") === "search-help"
    ? <V2SearchHelpView />
    : <V2CompatibilityRoute />;
}
