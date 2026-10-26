import { useEffect, useMemo, useState } from "react";
import {
  Bell,
  CalendarBlank,
  Check,
  Database,
  EnvelopeSimple,
  Globe,
  Info,
  LinkSimple,
  MagnifyingGlass,
  Plus,
  SlidersHorizontal,
  Sparkle,
  Star,
  Tag,
  X,
} from "@phosphor-icons/react";
import { Link } from "react-router-dom";

import type {
  ApiTopicDashboardResponse,
  SettingsStatusResponse,
  SourceProviderRegistryResponse,
  TopicNotificationSettings,
  TopicProfile,
  TopicSchedule,
  TopicSourceSettings,
} from "../../lib/api";
import { ACCENTS, DEFAULT_APPEARANCE, MOTIONS, THEMES, formatTimestamp, type AccentPreference, type MotionPreference, type ThemePreference, type TimeFormatPreference } from "../appearance";
import { useAppearance } from "../AppearanceProvider";
import { V2Button, V2Panel, V2Toggle, V2Unavailable } from "../V2Primitives";
import { ModelConnectionsPanel } from "../components/ModelConnectionsPanel";

export type V2SettingsSavePayload = {
  title: string;
  description: string;
  queries: string[];
  collectionCadence: string;
  reportCadence: string;
  acquisitionLanguage: string;
  reviewReportLanguage: string;
  acquisitionStartDate: string | null;
  acquisitionEndDate: string | null;
  sourceSettings: TopicSourceSettings | null;
  minRelevance: number;
  notification: {
    inAppEnabled: boolean;
    rules: Array<{ rule_id: string; enabled: boolean }>;
    channels: Array<{ channel_id: "email" | "webhook"; enabled: boolean; destination: string }>;
  } | null;
};

const WEIGHT_DEFINITIONS = [
  { key: "relevance", label: "관련성 (Relevance)", icon: MagnifyingGlass, tone: 1 },
  { key: "citations", label: "인용 영향력 (Citation Impact)", icon: Sparkle, tone: 2 },
  { key: "recency", label: "최신성 (Recency)", icon: CalendarBlank, tone: 3 },
  { key: "novelty", label: "새로움 (Novelty)", icon: Star, tone: 4 },
  { key: "industry", label: "산업 적중성 (Industry Relevance)", icon: Tag, tone: 5 },
  { key: "benchmark", label: "벤치마크 중요도 (Benchmark)", icon: Database, tone: 6 },
] as const;

type WeightKey = typeof WEIGHT_DEFINITIONS[number]["key"];

export function V2SettingsView({
  topicId,
  dashboard,
  profile,
  providers,
  notificationSettings,
  schedules,
  settingsStatus,
  busy,
  onSave,
}: {
  topicId: string;
  dashboard: ApiTopicDashboardResponse;
  profile: TopicProfile | null;
  providers: SourceProviderRegistryResponse | null;
  notificationSettings: TopicNotificationSettings | null;
  schedules: TopicSchedule[];
  settingsStatus: SettingsStatusResponse | null;
  busy: boolean;
  onSave: (payload: V2SettingsSavePayload) => Promise<void>;
}) {
  const { preferences, patchAppearance, updateAppearance, systemAccentAvailable } = useAppearance();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [queries, setQueries] = useState<string[]>([]);
  const [newQuery, setNewQuery] = useState("");
  const [collectionCadence, setCollectionCadence] = useState("manual");
  const [reportCadence, setReportCadence] = useState("weekly");
  const [acquisitionLanguage, setAcquisitionLanguage] = useState("any");
  const [reviewReportLanguage, setReviewReportLanguage] = useState("en");
  const [acquisitionStartDate, setAcquisitionStartDate] = useState("");
  const [acquisitionEndDate, setAcquisitionEndDate] = useState("");
  const [sourceEnabled, setSourceEnabled] = useState<Record<string, boolean>>({});
  const [weights, setWeights] = useState<Record<WeightKey, number>>({ relevance: 30, citations: 20, recency: 15, novelty: 15, industry: 10, benchmark: 10 });
  const [inAppEnabled, setInAppEnabled] = useState(true);
  const [ruleEnabled, setRuleEnabled] = useState<Record<string, boolean>>({});
  const [channelEnabled, setChannelEnabled] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setTitle(profile?.title || dashboard.topic?.name || topicId);
    setDescription(profile?.description || dashboard.topic?.description || "");
    setQueries(profile?.queries ?? dashboard.researchOperations.operationsPlanner.topicConfiguration.queryTerms ?? []);
    setCollectionCadence(profile?.default_collection_cadence ?? "manual");
    setReportCadence(profile?.default_report_cadence ?? "weekly");
    setAcquisitionLanguage(profile?.acquisition_language ?? "any");
    setReviewReportLanguage(profile?.review_report_language ?? "en");
    setAcquisitionStartDate(profile?.acquisition_start_date ?? "");
    setAcquisitionEndDate(profile?.acquisition_end_date ?? "");
    const nextSources: Record<string, boolean> = {};
    providers?.items.forEach((provider) => {
      nextSources[provider.provider] = profile?.source_settings.enabledProviders.includes(provider.provider as never) ?? provider.available;
    });
    setSourceEnabled(nextSources);
    setWeights({ relevance: 30, citations: 20, recency: 15, novelty: 15, industry: 10, benchmark: 10 });
    setInAppEnabled(notificationSettings?.in_app_enabled ?? true);
    setRuleEnabled(Object.fromEntries((notificationSettings?.rules ?? []).map((rule) => [rule.rule_id, rule.enabled])));
    setChannelEnabled(Object.fromEntries((notificationSettings?.channels ?? []).map((channel) => [channel.channel_id, channel.enabled])));
    setNotice(null);
  }, [dashboard, notificationSettings, profile, providers, topicId]);

  const weightTotal = Object.values(weights).reduce((sum, value) => sum + value, 0);
  const sourceRows = useMemo(() => {
    const actual = providers?.items ?? [];
    const future = [
      { provider: "semantic_scholar", displayName: "Semantic Scholar", description: "연결되지 않음", available: false, supported: false },
      { provider: "pubmed", displayName: "PubMed", description: "연결되지 않음", available: false, supported: false },
      { provider: "google_scholar", displayName: "Google Scholar", description: "연결되지 않음", available: false, supported: false },
      { provider: "web_monitoring", displayName: "웹 모니터링 (뉴스/블로그)", description: "연결되지 않음", available: false, supported: false },
    ];
    return [...actual, ...future.filter((item) => !actual.some((actualItem) => actualItem.provider === item.provider))];
  }, [providers]);

  function buildSourceSettings() {
    if (!profile?.source_settings) return null;
    const enabledProviders = profile.source_settings.enabledProviders.filter((provider) => sourceEnabled[provider]);
    const providersMap = Object.fromEntries(Object.entries(profile.source_settings.providers).map(([key, value]) => [key, value ? { ...value, enabled: Boolean(sourceEnabled[key]) } : value]));
    return { ...profile.source_settings, enabledProviders, providers: providersMap } as TopicSourceSettings;
  }

  function reset() {
    setTitle(profile?.title || dashboard.topic?.name || topicId);
    setDescription(profile?.description || dashboard.topic?.description || "");
    setQueries(profile?.queries ?? []);
    setCollectionCadence(profile?.default_collection_cadence ?? "manual");
    setReportCadence(profile?.default_report_cadence ?? "weekly");
    setAcquisitionLanguage(profile?.acquisition_language ?? "any");
    setReviewReportLanguage(profile?.review_report_language ?? "en");
    setAcquisitionStartDate(profile?.acquisition_start_date ?? "");
    setAcquisitionEndDate(profile?.acquisition_end_date ?? "");
    setNotice("저장되지 않은 변경사항을 초기화했습니다.");
  }

  async function save() {
    await onSave({
      title: title.trim() || topicId,
      description: description.trim(),
      queries,
      collectionCadence,
      reportCadence,
      acquisitionLanguage,
      reviewReportLanguage,
      acquisitionStartDate: acquisitionStartDate || null,
      acquisitionEndDate: acquisitionEndDate || null,
      sourceSettings: buildSourceSettings(),
      minRelevance: dashboard.researchOperations.operationsPlanner.topicConfiguration.minRelevance,
      notification: notificationSettings ? {
        inAppEnabled,
        rules: notificationSettings.rules.map((rule) => ({ rule_id: rule.rule_id, enabled: Boolean(ruleEnabled[rule.rule_id]) })),
        channels: notificationSettings.channels.filter((channel) => channel.channel_id === "email" || channel.channel_id === "webhook").map((channel) => ({ channel_id: channel.channel_id as "email" | "webhook", enabled: Boolean(channelEnabled[channel.channel_id]), destination: channel.destination })),
      } : null,
    });
    setNotice("지원되는 주제·수집·알림 설정을 저장했습니다. Appearance 설정은 이 기기의 브라우저/desktop 저장소에 즉시 유지됩니다.");
  }

  return (
    <div className="v2-settings-screen">
      {notice ? <p className="v2-inline-notice" role="status">{notice}</p> : null}
      <section className="v2-settings-top-grid">
        <V2Panel title="주제 기본 설정" action={<Info size={14} />}>
          <div className="v2-form-grid">
            <label>주제 이름<input value={title} onChange={(event) => setTitle(event.target.value)} /></label>
            <label>설명<textarea value={description} maxLength={300} onChange={(event) => setDescription(event.target.value)} /><small>{description.length}/300</small></label>
            <label>수집 언어<select aria-label="수집 언어" value={acquisitionLanguage} onChange={(event) => setAcquisitionLanguage(event.target.value)}><option value="any">모든 언어 (any)</option><option value="auto">소스 자동 (auto)</option><option value="en">영어 (English)</option><option value="ko">한국어 (Korean)</option></select><small>소스가 지원하는 경우에만 수집 필터로 적용됩니다.</small></label>
            <label>보고서 출력 언어<select aria-label="보고서 출력 언어" value={reviewReportLanguage} onChange={(event) => setReviewReportLanguage(event.target.value)}><option value="en">영어 (English)</option><option value="ko">한국어 (Korean)</option><option value="en-US">영어 미국식 (en-US)</option></select><small>UI 언어·자동 번역과 별개의 출력 정책입니다.</small></label>
            <label>수집 주기<select value={collectionCadence} onChange={(event) => setCollectionCadence(event.target.value)}><option value="manual">수동</option><option value="daily">매일</option><option value="weekly">매주</option><option value="monthly">매월</option></select></label>
            <label>수집 시작일<input aria-label="수집 시작일" type="date" value={acquisitionStartDate} onChange={(event) => setAcquisitionStartDate(event.target.value)} /><small>선택 사항 · YYYY-MM-DD 포함 범위</small></label>
            <label>수집 종료일<input aria-label="수집 종료일" type="date" value={acquisitionEndDate} onChange={(event) => setAcquisitionEndDate(event.target.value)} /><small>시작일 이후 또는 공란</small></label>
            <label>보고 주기<select value={reportCadence} onChange={(event) => setReportCadence(event.target.value)}><option value="weekly">매주</option><option value="monthly">매월</option><option value="manual">수동</option></select></label>
          </div>
        </V2Panel>

        <V2Panel title="데이터 수집 소스" action={<Info size={14} />}>
          <div className="v2-source-list">
            {sourceRows.slice(0, 6).map((provider, index) => (
              <article key={provider.provider}>
                <span className={`tone-${(index % 6) + 1}`}><Globe size={16} /></span>
                <div><strong>{provider.displayName}</strong><small>{provider.available ? "연결됨" : provider.description}</small></div>
                <V2Toggle checked={Boolean(sourceEnabled[provider.provider]) && provider.available} disabled={!provider.available || !provider.supported} label={`${provider.displayName} 활성화`} onChange={(enabled) => setSourceEnabled((current) => ({ ...current, [provider.provider]: enabled }))} />
              </article>
            ))}
          </div>
          <V2Button className="v2-full-button" disabled title="새 source connector 추가는 M2 이후 범위입니다."><Plus size={14} />새 데이터 소스 · M2</V2Button>
        </V2Panel>

        <V2Panel title="분석 기준 / 점수 가중치" action={<Info size={14} />}>
          <div className="v2-weight-list">
            {WEIGHT_DEFINITIONS.map((definition) => {
              const Icon = definition.icon;
              return <label key={definition.key}><span className={`tone-${definition.tone}`}><Icon size={15} /></span><strong>{definition.label}</strong><input aria-label={`${definition.label} (편집 불가)`} type="range" min="0" max="60" step="5" value={weights[definition.key]} disabled title="가중치 저장/실행 계약은 M3 범위입니다." /><em>{weights[definition.key]}%</em></label>;
            })}
          </div>
          <div className="v2-weight-total is-valid"><span>참고 프리셋 · M3 (저장되지 않음)</span><strong>{weightTotal}%</strong></div>
        </V2Panel>

        <V2Panel title="알림 및 보고서 자동화" action={<Info size={14} />}>
          <div className="v2-automation-settings">
            <article><span className="tone-1"><CalendarBlank size={16} /></span><div><strong>주간 트렌드 알림</strong><small>{profile?.default_report_cadence ?? "weekly"}</small></div><V2Toggle checked={inAppEnabled} label="인앱 알림" onChange={setInAppEnabled} /></article>
            {(notificationSettings?.rules ?? []).slice(0, 3).map((rule, index) => <article key={rule.rule_id}><span className={`tone-${index + 2}`}><Bell size={16} /></span><div><strong>{rule.label}</strong><small>{rule.description}</small></div><V2Toggle checked={Boolean(ruleEnabled[rule.rule_id])} label={rule.label} onChange={(enabled) => setRuleEnabled((current) => ({ ...current, [rule.rule_id]: enabled }))} /></article>)}
            {(notificationSettings?.channels ?? []).map((channel, index) => <article key={channel.channel_id}><span className={`tone-${index + 4}`}><EnvelopeSimple size={16} /></span><div><strong>{channel.label}</strong><small>{channel.destination_label || channel.status_label}</small></div><V2Toggle checked={Boolean(channelEnabled[channel.channel_id]) && channel.available} disabled={!channel.available} label={channel.label} onChange={(enabled) => setChannelEnabled((current) => ({ ...current, [channel.channel_id]: enabled }))} /></article>)}
            {!notificationSettings ? <p className="v2-settings-note">이 주제의 알림 설정 API가 아직 준비되지 않았습니다.</p> : null}
          </div>
          <Link className="v2-text-link" to="/settings/notifications?section=notifications">알림 설정 상세 · M5 <ArrowRightIcon /></Link>
        </V2Panel>
      </section>

      <ModelConnectionsPanel/>

      <section className="v2-settings-bottom-grid">
        <V2Panel title="태그 / 키워드 관리" action={<Info size={14} />}>
          <label className="v2-keyword-add">새 키워드 추가<span><input value={newQuery} onChange={(event) => setNewQuery(event.target.value)} placeholder="새 키워드를 입력하세요..." /><V2Button onClick={() => { const value = newQuery.trim(); if (value && !queries.includes(value)) setQueries([...queries, value]); setNewQuery(""); }}><Plus size={14} />추가</V2Button></span></label>
          <div className="v2-keyword-chips">{queries.map((query, index) => <button type="button" className={`tone-${(index % 5) + 1}`} key={query} onClick={() => setQueries(queries.filter((item) => item !== query))}>{query}<X size={12} /></button>)}</div>
          <p className="v2-settings-note">추천 키워드: Mixture of Experts · In-Context Learning · RLHF · Tool Use · Multimodal</p>
        </V2Panel>

        <V2Panel title="제품 세션 현재 상태">
          <dl className="v2-capability-facts">
            <div><dt>제품 세션</dt><dd>{settingsStatus ? (settingsStatus.authenticated ? "인증됨" : "인증되지 않음") : "상태 확인 불가"}</dd></div>
          </dl>
          <V2Unavailable title="팀 협업 및 RBAC는 제공되지 않습니다." reason="M1은 로컬 단일 사용자 workspace이며 팀 멤버·역할·권한 UI를 생성하지 않습니다." />
        </V2Panel>

        <V2Panel title="시각화 및 UI 환경" action={<Info size={14} />}>
          <div className="v2-ui-settings">
            <label>테마<select aria-label="테마" value={preferences.theme} onChange={(event) => patchAppearance({ theme: event.target.value as ThemePreference })}>{THEMES.map((theme) => <option key={theme} value={theme}>{theme}</option>)}</select></label>
            <label>강조색<select aria-label="강조색" value={preferences.accent} onChange={(event) => patchAppearance({ accent: event.target.value as AccentPreference })}>{ACCENTS.map((accent) => <option key={accent} value={accent} disabled={accent === "system" && !systemAccentAvailable}>{accent === "system" && !systemAccentAvailable ? "system (플랫폼 증명 없음)" : accent}</option>)}</select></label>
            <label>화면 밀도<select aria-label="화면 밀도" value={preferences.density} onChange={(event) => patchAppearance({ density: event.target.value as "compact" | "comfortable" })}><option value="compact">compact</option><option value="comfortable">comfortable</option></select></label>
            <label>동작 감소<select aria-label="동작 설정" value={preferences.motion} onChange={(event) => patchAppearance({ motion: event.target.value as MotionPreference })}>{MOTIONS.map((motion) => <option key={motion} value={motion}>{motion}</option>)}</select></label>
            <label>기본 차트 유형<select defaultValue="current" disabled title="차트 유형 preference 계약은 M3 범위입니다."><option value="current">현재 화면 정의 · M3</option></select></label>
            <label>UI 언어<select defaultValue="ko" disabled title="UI 국제화는 M2 이후 범위입니다."><option value="ko">한국어 (현재 고정)</option></select></label>
            <label>시간 표시 형식<select aria-label="시간 표시 형식" value={preferences.timeFormat ?? "locale"} onChange={(event) => patchAppearance({ timeFormat: event.target.value as TimeFormatPreference })}><option value="locale">시스템 locale</option><option value="12h">12시간 (AM/PM)</option><option value="24h">24시간</option></select><small>예시: {formatTimestamp(new Date(), preferences.timeFormat ?? "locale", "ko-KR")}</small></label>
          </div>
          <V2Button onClick={() => updateAppearance(DEFAULT_APPEARANCE)}>Appearance 기본값 복원</V2Button>
        </V2Panel>
      </section>

      <footer className="v2-settings-savebar">
        <span><Check size={16} weight="bold" />지원되는 변경사항은 저장 버튼으로 확정됩니다.</span>
        <div><V2Button className="is-primary" busy={busy} onClick={() => void save()}>지원 설정 저장</V2Button><V2Button onClick={reset}>주제 변경 초기화</V2Button></div>
      </footer>
    </div>
  );
}

function ArrowRightIcon() {
  return <LinkSimple size={13} />;
}
