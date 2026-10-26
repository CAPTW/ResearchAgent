import { useEffect, useMemo, useRef, useState } from "react";
import { RecommendationHistoryPanel } from "./RecommendationHistoryPanel";
import {
  ArrowRight,
  Brain,
  FileText,
  Lightning,
  Sparkle,
} from "@phosphor-icons/react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ApiTopicDashboardResponse, ApiTopicRecommendationsResponse, TopicAction } from "../../lib/api";
import { V2Button, V2Empty, V2Panel, V2_CHART_COLORS, V2_TOOLTIP_STYLE, readV2ChartColor } from "../V2Primitives";

type ClusterNode = {
  id: string;
  label: string;
  color: string;
  size: number;
  fx: number;
  fy: number;
  kind: "topic" | "keyword";
};

type ClusterLink = {
  source: string;
  target: string;
  color: string;
};

function wrapNodeLabel(label: string, maxCharacters: number) {
  const words = label.split(/\s+/).filter(Boolean);
  if (words.length <= 1) return [label.length > maxCharacters ? `${label.slice(0, maxCharacters - 1)}…` : label];
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (`${current} ${word}`.trim().length <= maxCharacters) current = `${current} ${word}`.trim();
    else {
      if (current) lines.push(current);
      current = word;
    }
    if (lines.length === 2) break;
  }
  if (current && lines.length < 2) lines.push(current);
  return lines.slice(0, 2);
}

function TopicClusterGraph({ topic, keywords }: { topic: string; keywords: Array<{ keyword: string; score: number; paperCount: number }> }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<ForceGraphMethods<ClusterNode, ClusterLink> | undefined>(undefined);
  const [size, setSize] = useState({ width: 360, height: 286 });

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const updateSize = () => setSize({ width: Math.max(260, host.clientWidth), height: Math.max(240, host.clientHeight) });
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const graphData = useMemo(() => {
    const visible = keywords.slice(0, 8);
    const nodes: ClusterNode[] = [{
      id: "topic",
      label: topic,
      color: readV2ChartColor(0),
      size: 21,
      fx: 0,
      fy: 0,
      kind: "topic",
    }];
    const links: ClusterLink[] = [];
    visible.forEach((keyword, index) => {
      const angle = -Math.PI / 2 + (index * Math.PI * 2) / Math.max(visible.length, 1);
      const radius = index % 2 === 0 ? 78 : 92;
      const id = `keyword-${index}`;
      nodes.push({
        id,
        label: keyword.keyword,
        color: readV2ChartColor(index + 1),
        size: Math.max(9, Math.min(16, 9 + keyword.paperCount * 0.7 + keyword.score / 28)),
        fx: Math.cos(angle) * radius,
        fy: Math.sin(angle) * radius,
        kind: "keyword",
      });
      links.push({ source: "topic", target: id, color: `${readV2ChartColor(index + 1)}80` });
      if (index > 0 && index % 2 === 0) links.push({ source: `keyword-${index - 1}`, target: id, color: "rgba(93, 119, 145, 0.55)" });
    });
    return { nodes, links };
  }, [keywords, topic]);

  return (
    <div ref={hostRef} className="v2-cluster-chart" role="img" aria-label={`${topic} 토픽과 ${graphData.nodes.length - 1}개 핵심 키워드의 관계망`}>
      <ForceGraph2D<ClusterNode, ClusterLink>
        ref={graphRef}
        width={size.width}
        height={size.height}
        graphData={graphData}
        backgroundColor="rgba(0,0,0,0)"
        nodeId="id"
        nodeLabel="label"
        nodeVal="size"
        nodeColor="color"
        linkColor="color"
        linkWidth={1.2}
        enableNodeDrag={false}
        enablePanInteraction={false}
        enableZoomInteraction={false}
        minZoom={0.5}
        maxZoom={2}
        warmupTicks={1}
        cooldownTicks={1}
        onEngineStop={() => graphRef.current?.zoomToFit(0, 26)}
        nodeCanvasObject={(node, context, globalScale) => {
          const radius = node.size;
          context.beginPath();
          context.arc(node.x ?? 0, node.y ?? 0, radius, 0, Math.PI * 2);
          context.fillStyle = `${node.color}${node.kind === "topic" ? "de" : "bd"}`;
          context.fill();
          context.lineWidth = node.kind === "topic" ? 2.2 / globalScale : 1.4 / globalScale;
          context.strokeStyle = node.color;
          context.stroke();
          const fontSize = (node.kind === "topic" ? 6.3 : 4.5) / globalScale;
          context.font = `600 ${fontSize}px Inter, system-ui, sans-serif`;
          context.textAlign = "center";
          context.textBaseline = "middle";
          context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--text-inverse").trim() || "#f3f7ff";
          const lines = wrapNodeLabel(node.label, node.kind === "topic" ? 17 : 12);
          lines.forEach((line, lineIndex) => {
            context.fillText(line, node.x ?? 0, (node.y ?? 0) + (lineIndex - (lines.length - 1) / 2) * fontSize * 1.15);
          });
        }}
      />
      <ul className="v2-visually-hidden">{graphData.nodes.slice(1).map((node) => <li key={node.id}>{topic} — {node.label}</li>)}</ul>
    </div>
  );
}

export function V2InsightsView({
  topicId,
  dashboard,
  recommendations,
  onRecommendationsChange,
  busyAction,
  onRunAction,
}: {
  topicId: string;
  dashboard: ApiTopicDashboardResponse;
  recommendations: ApiTopicRecommendationsResponse | null;
  onRecommendationsChange: (data:ApiTopicRecommendationsResponse)=>void;
  busyAction: TopicAction | null;
  onRunAction: (action: Exclude<TopicAction, "full_pipeline">) => void;
}) {
  const analytics = dashboard.analytics;
  const keywordDetails = analytics?.keywordDetails ?? dashboard.hotKeywords.map((keyword) => ({
    keyword: keyword.keyword,
    score: keyword.score,
    paperCount: keyword.paperCount ?? 0,
    sampleTitles: [],
    explanation: keyword.explanation ?? "현재 주제에서 관측된 핵심 키워드입니다.",
  }));
  const methods = (analytics?.fieldDetails ?? dashboard.fieldDistribution).slice(0, 6).map((item) => ({ name: item.label, value: item.count }));
  const institutions = (analytics?.institutionDetails ?? dashboard.institutionDistribution).slice(0, 5).map((item) => ({ name: item.name, value: item.count }));
  const recItems = recommendations?.items ?? [];
  const insightCount = keywordDetails.length + recItems.length + (analytics?.coverage.length ?? 0);

  return (
    <div className="v2-screen-stack">
      <section className="v2-kpi-row" aria-label="현재 응답 항목">
        <article className="v2-panel"><p>현재 응답 항목 수</p><strong>{insightCount}</strong><p>키워드·추천·coverage 항목 합계</p></article>
        <article className="v2-panel"><p>추천 응답 항목 수</p><strong>{recommendations ? recItems.length : "사용 불가"}</strong><p>{recommendations ? "현재 추천 응답 기준" : "추천 리소스 없음"}</p></article>
      </section>

      <section className="v2-insights-grid">
        <V2Panel title="핵심 발견" className="v2-discoveries-panel">
          <div className="v2-discoveries">
            {keywordDetails.slice(0, 5).map((item, index) => (
              <article key={item.keyword} className={`tone-${(index % 5) + 1}`}>
                <span><Sparkle size={16} weight="duotone" /></span>
                <div><strong>{item.keyword}</strong><p>{item.explanation}</p></div>
                <em>점수 {Math.round(item.score)}</em>
              </article>
            ))}
            {!keywordDetails.length ? <V2Empty title="발견된 인사이트가 없습니다." description="분석 실행 후 핵심 발견이 표시됩니다." /> : null}
          </div>
        </V2Panel>

        <V2Panel title="키워드 방사형 인덱스" subtitle="선은 통계적 연관성이 아니라 화면 배치 연결선입니다." className="v2-cluster-panel">
          <TopicClusterGraph topic={dashboard.topic?.name ?? "Research Topic"} keywords={keywordDetails} />
        </V2Panel>

        <V2Panel title="연구 방법 분포">
          <div className="v2-donut-layout">
            <div className="v2-donut-chart">
              <ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={methods} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%" isAnimationActive={false}>{methods.map((item, index) => <Cell key={item.name} fill={V2_CHART_COLORS[index % V2_CHART_COLORS.length]} />)}</Pie><Tooltip contentStyle={V2_TOOLTIP_STYLE} /></PieChart></ResponsiveContainer>
              <strong>{dashboard.metrics.collectedPapers.value.toLocaleString()}<small>Total</small></strong>
            </div>
            <ul>{methods.map((item, index) => <li key={item.name}><i style={{ backgroundColor: V2_CHART_COLORS[index % V2_CHART_COLORS.length] }} /><span>{item.name}</span><strong>{item.value}</strong></li>)}</ul>
          </div>
        </V2Panel>

        <V2Panel title="기관명 출현 횟수" subtitle="영향력 순위가 아니라 현재 분석 레코드의 occurrence count입니다.">
          <div className="v2-chart-medium">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={institutions} layout="vertical" margin={{ left: 10, right: 12 }}>
                <XAxis type="number" hide />
                <YAxis dataKey="name" type="category" width={120} axisLine={false} tickLine={false} tick={{ fill: "var(--text-secondary)", fontSize: 9 }} />
                <Tooltip contentStyle={V2_TOOLTIP_STYLE} />
                <Bar dataKey="value" fill="var(--chart-series-1)" radius={[3, 3, 3, 3]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </V2Panel>

        <V2Panel title="연구 공백·후속 주제와 검토 이력" className="v2-recommendation-history-panel">
          <RecommendationHistoryPanel key={topicId} topicId={topicId} recommendations={recommendations} onRecommendationsChange={onRecommendationsChange}/>
        </V2Panel>

        <V2Panel title="현재 실행 가능한 액션">
          <div className="v2-recommended-actions">
            <V2Button busy={busyAction === "analyze"} onClick={() => onRunAction("analyze")}><Brain size={16} />최신 동향 심층 분석 <ArrowRight size={14} /></V2Button>
            <V2Button busy={busyAction === "collect"} onClick={() => onRunAction("collect")}><Lightning size={16} />근거 수집 범위 확장 <ArrowRight size={14} /></V2Button>
            <V2Button busy={busyAction === "report"} onClick={() => onRunAction("report")}><FileText size={16} />인사이트 보고서 생성 <ArrowRight size={14} /></V2Button>
          </div>
        </V2Panel>
      </section>
    </div>
  );
}
