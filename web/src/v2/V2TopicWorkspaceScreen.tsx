import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, NavLink, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowClockwise, Gear, Star } from "@phosphor-icons/react";

import {
  getApiBase,
  getApiReports,
  getApiTopicDashboard,
  getApiTopicRecommendations,
  getApiTopics,
  getSettingsStatus,
  getSourceProviders,
  getTopicNotificationSettings,
  getTopicProfiles,
  getTopicSchedules,
  runApiTaskNow,
  runApiTopicAction,
  saveTopicConfiguration,
  updateApiTask,
  updateTopicNotificationSettings,
  updateTopicProfile,
  updateTopicSchedule,
  type ApiReportsResponse,
  type ApiScheduledTask,
  type ApiTopicDashboardResponse,
  type ApiTopicRecommendationsResponse,
  type SettingsStatusResponse,
  type SourceProviderRegistryResponse,
  type TopicAction,
  type TopicNotificationSettings,
  type TopicProfile,
  type TopicSchedule,
} from "../lib/api";
import {
  addPaperToCollection,
  bulkSavePapersToCollection,
  createPaperCollection,
  deletePaperCollection,
  exportTopicPaperBank,
  getPaperCollections,
  getTopicPaperBank,
  removePaperFromCollection,
  renamePaperCollection,
  type PaperBankCollectionList,
  type PaperBankExport,
  type PaperBankResponse,
  type PaperBulkSaveResult,
} from "./paperBankApi";
import {
  SAVED_PAPERS_COLLECTION_ID,
  collectionIdToRouteValue,
  createPapersRefreshState,
  paperCollectionLabel,
  parsePaperWorkspaceRoute,
  type PapersRefreshState,
} from "./paperWorkspaceState";
import { V2Button, V2Error, V2Loading, V2Unavailable } from "./V2Primitives";
import { V2CompatibilityRoute } from "./routeIntent";
import {
  failResource,
  initialResourceState,
  resolveResource,
  retryResource,
  startResource,
  unavailableResource,
  type ResourceState,
} from "./resourceState";
import { V2DashboardView } from "./views/V2DashboardView";
import { V2InsightsView } from "./views/V2InsightsView";
import { TemporalResearchPanel } from "./views/TemporalResearchPanel";
import { V2PapersView } from "./views/V2PapersView";
import { V2ReportsView } from "./views/V2ReportsView";
import { captureSynthesis, type SynthesisSelection } from "../lib/localSynthesis";
import { LocalSynthesisPanel } from "./views/LocalSynthesisPanel";
import { JournalLibraryPanel } from "./views/JournalLibraryPanel";
import { V2SettingsView, type V2SettingsSavePayload } from "./views/V2SettingsView";
import { formatTimestamp } from "./appearance";
import { useOptionalAppearance } from "./AppearanceProvider";

const VALID_TABS = new Set(["dashboard", "papers", "insights", "reports", "settings"]);
type WorkspaceTab = "dashboard" | "papers" | "insights" | "reports" | "settings";
type PaperCollectionRouteAvailability = "idle" | "loading" | "available" | "missing" | "malformed";
type PaperResourceLoadResult = {
  generation: number;
  committed: boolean;
  bankSucceeded: boolean;
  failureReason: string | null;
};

function createResourceState<T>(): ResourceState<T> {
  return { ...initialResourceState } as ResourceState<T>;
}

function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function formatUpdatedAt(value: string | null | undefined, timeFormat: "locale" | "12h" | "24h" = "locale") {
  if (!value) return "업데이트 기록 없음";
  return formatTimestamp(value, timeFormat, "ko-KR");
}

function resolveProfile(topicId: string, dashboard: ApiTopicDashboardResponse | null, profiles: TopicProfile[]) {
  return profiles.find((profile) => profile.profile_id === topicId)
    ?? profiles.find((profile) => profile.title === dashboard?.topic?.name || profile.topic === dashboard?.topic?.name)
    ?? null;
}

export function V2TopicWorkspaceScreen() {
  const { topicId } = useParams<{ topicId: string }>();
  return <TopicWorkspace key={topicId ?? ""} />;
}

function TopicWorkspace() {
  const { topicId: rawTopicId, tab: rawTab } = useParams<{ topicId: string; tab: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const topicId = rawTopicId ? decodeURIComponent(rawTopicId) : "";
  const tab = rawTab && VALID_TABS.has(rawTab) ? rawTab as WorkspaceTab : "dashboard";
  const paperRoute = useMemo(() => parsePaperWorkspaceRoute(searchParams), [searchParams]);
  const { preferences } = useOptionalAppearance();
  const currentLocation = useRef({ topicId, tab });
  currentLocation.current = { topicId, tab };
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function freezeSelectedPapers(selection: SynthesisSelection[], requestId: string) {
    if (!mounted.current || currentLocation.current.topicId !== topicId || currentLocation.current.tab !== "papers" || paperBank?.topicId !== topicId) {
      throw new Error("주제가 바뀌었습니다. 현재 주제의 논문을 다시 선택하세요.");
    }
    const { job } = await captureSynthesis(topicId, selection, requestId);
    if (mounted.current && currentLocation.current.topicId === topicId && currentLocation.current.tab === "papers") {
      navigate(`/topics/${encodeURIComponent(topicId)}/reports?synthesis=${encodeURIComponent(job.job_id)}`);
    }
  }

  const [dashboard, setDashboard] = useState<ApiTopicDashboardResponse | null>(null);
  const [paperBank, setPaperBank] = useState<PaperBankResponse | null>(null);
  const [paperCollections, setPaperCollections] = useState<PaperBankCollectionList | null>(null);
  const [reports, setReports] = useState<ApiReportsResponse | null>(null);
  const [recommendations, setRecommendations] = useState<ApiTopicRecommendationsResponse | null>(null);
  const [profiles, setProfiles] = useState<TopicProfile[]>([]);
  const [schedules, setSchedules] = useState<TopicSchedule[]>([]);
  const [providers, setProviders] = useState<SourceProviderRegistryResponse | null>(null);
  const [settingsStatus, setSettingsStatus] = useState<SettingsStatusResponse | null>(null);
  const [notificationSettings, setNotificationSettings] = useState<TopicNotificationSettings | null>(null);
  const [baseState, setBaseState] = useState<ResourceState<ApiTopicDashboardResponse>>(createResourceState);
  const [profileState, setProfileState] = useState<ResourceState<TopicProfile[]>>(createResourceState);
  const [sectionStates, setSectionStates] = useState<Record<WorkspaceTab, ResourceState<boolean>>>(() => ({
    dashboard: createResourceState<boolean>(),
    papers: createResourceState<boolean>(),
    insights: createResourceState<boolean>(),
    reports: createResourceState<boolean>(),
    settings: createResourceState<boolean>(),
  }));
  const [notificationState, setNotificationState] = useState<ResourceState<TopicNotificationSettings>>(createResourceState);
  const [collectionState, setCollectionState] = useState<ResourceState<PaperBankCollectionList>>(createResourceState);
  const [saveTargetCollectionId, setSaveTargetCollectionId] = useState(SAVED_PAPERS_COLLECTION_ID);
  const [paperCollectionRouteAvailability, setPaperCollectionRouteAvailability] = useState<PaperCollectionRouteAvailability>("idle");
  const [paperRefreshState, setPaperRefreshState] = useState<PapersRefreshState>(createPapersRefreshState);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<TopicAction | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [busyScheduleId, setBusyScheduleId] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const busyActionRef = useRef<TopicAction | null>(null);
  const paperRefreshBusyRef = useRef(false);
  const paperRequestGenerationRef = useRef(0);
  const paperRequestInFlightRef = useRef<{ key: string; promise: Promise<PaperResourceLoadResult> } | null>(null);

  const paperQuery = paperRoute.query;
  const paperProvider = paperRoute.provider;
  const paperCollection = paperRoute.collection.collectionId;
  const paperReviewState = paperRoute.reviewState;
  const paperYear = paperRoute.year;
  const paperOpenAccess = paperRoute.openAccess;
  const paperSort = paperRoute.sort;
  const paperPage = paperRoute.page;
  const openPaperVersionId = paperRoute.openPaperVersionId;
  const openEvidenceId = paperRoute.openEvidenceId;
  const profile = useMemo(() => resolveProfile(topicId, dashboard, profiles), [dashboard, profiles, topicId]);
  const topicSchedules = useMemo(() => schedules.filter((schedule) => schedule.profile_id === profile?.profile_id || schedule.topic === profile?.topic || schedule.topic === dashboard?.topic?.name), [dashboard?.topic?.name, profile, schedules]);

  const loadBase = useCallback(async () => {
    if (!topicId) return;
    setBaseState((current) => current.phase === "error" ? retryResource(current) : startResource(current));
    setProfileState((current) => current.phase === "error" ? retryResource(current) : startResource(current));
    const [dashboardResult, profileResult] = await Promise.allSettled([
      getApiTopicDashboard(topicId),
      getTopicProfiles(),
    ]);
    if (dashboardResult.status === "fulfilled") {
      setDashboard(dashboardResult.value);
      setBaseState((current) => resolveResource(current, dashboardResult.value));
      setSectionStates((current) => ({ ...current, dashboard: resolveResource(current.dashboard, true) }));
    } else {
      setBaseState((current) => failResource(current, dashboardResult.reason));
      setSectionStates((current) => ({ ...current, dashboard: failResource(current.dashboard, dashboardResult.reason) }));
    }
    if (profileResult.status === "fulfilled") {
      setProfiles(profileResult.value.items);
      setProfileState((current) => resolveResource(current, profileResult.value.items));
    } else {
      setProfileState((current) => failResource(current, profileResult.reason));
    }
  }, [topicId]);

  useEffect(() => {
    void loadBase();
  }, [loadBase]);

  const loadPaperResources = useCallback((collectionId = paperCollection): Promise<PaperResourceLoadResult> => {
    const requestKey = JSON.stringify([
      topicId,
      paperQuery,
      paperProvider,
      collectionId,
      paperReviewState,
      paperYear,
      paperOpenAccess,
      paperSort,
      paperPage,
      paperRoute.collection.kind,
    ]);
    const inFlight = paperRequestInFlightRef.current;
    if (inFlight?.key === requestKey) return inFlight.promise;

    const requestGeneration = paperRequestGenerationRef.current + 1;
    paperRequestGenerationRef.current = requestGeneration;
    const request = (async (): Promise<PaperResourceLoadResult> => {
      if (!topicId) {
        return { generation: requestGeneration, committed: true, bankSucceeded: false, failureReason: "topic_id_missing" };
      }
      if (paperRoute.collection.kind === "malformed" && collectionId === paperCollection) {
        setPaperCollectionRouteAvailability("malformed");
        setSectionStates((current) => ({
          ...current,
          papers: unavailableResource(current.papers, "paper_collection_route_malformed"),
        }));
        return { generation: requestGeneration, committed: true, bankSucceeded: false, failureReason: "paper_collection_route_malformed" };
      }

      setPaperCollectionRouteAvailability(paperRoute.collection.kind === "all" ? "idle" : "loading");
      setSectionStates((current) => ({
        ...current,
        papers: current.papers.phase === "error" ? retryResource(current.papers) : startResource(current.papers),
      }));
      setCollectionState((current) => current.phase === "error" ? retryResource(current) : startResource(current));

      const [bankResult, collectionResult] = await Promise.allSettled([
        getTopicPaperBank(topicId, {
          q: paperQuery,
          provider: paperProvider,
          collectionId,
          reviewState: paperReviewState,
          year: paperYear,
          openAccess: paperOpenAccess,
          sort: paperSort,
          page: paperPage,
          pageSize: 25,
        }),
        getPaperCollections(topicId),
      ]);

      if (requestGeneration !== paperRequestGenerationRef.current) {
        return { generation: requestGeneration, committed: false, bankSucceeded: false, failureReason: null };
      }

      let collectionFailureReason: string | null = null;
      if (collectionResult.status === "fulfilled") {
        setPaperCollections(collectionResult.value);
        setCollectionState((current) => resolveResource(current, collectionResult.value));
      } else {
        const message = getErrorMessage(collectionResult.reason, "paper_collection_store_unavailable");
        setCollectionState((current) => unavailableResource(current, message));
        collectionFailureReason = message;
      }

      if (bankResult.status === "rejected") {
        const message = getErrorMessage(bankResult.reason, "논문 목록을 불러오지 못했습니다.");
        const collectionMissing = paperRoute.collection.kind !== "all"
          && (message.includes("collection_not_found") || message.includes("paper_collection_not_found"));
        if (collectionMissing) {
          setPaperCollectionRouteAvailability("missing");
          setSectionStates((current) => ({
            ...current,
            papers: unavailableResource(current.papers, message),
          }));
        } else {
          setSectionStates((current) => ({ ...current, papers: failResource(current.papers, bankResult.reason) }));
        }
        return { generation: requestGeneration, committed: true, bankSucceeded: false, failureReason: message };
      }

      setPaperBank(bankResult.value);
      setPaperCollectionRouteAvailability(paperRoute.collection.kind === "all" ? "idle" : "available");
      setSectionStates((current) => ({ ...current, papers: resolveResource(current.papers, true) }));
      return {
        generation: requestGeneration,
        committed: true,
        bankSucceeded: true,
        failureReason: collectionFailureReason,
      };
    })();
    paperRequestInFlightRef.current = { key: requestKey, promise: request };
    void request.then(
      () => {
        if (paperRequestInFlightRef.current?.promise === request) paperRequestInFlightRef.current = null;
      },
      () => {
        if (paperRequestInFlightRef.current?.promise === request) paperRequestInFlightRef.current = null;
      },
    );
    return request;
  }, [paperCollection, paperPage, paperProvider, paperQuery, paperReviewState, paperRoute.collection.kind, paperOpenAccess, paperSort, paperYear, topicId]);

  useEffect(() => {
    if (tab === "papers") return;
    paperRequestGenerationRef.current += 1;
    paperRequestInFlightRef.current = null;
  }, [tab]);

  const loadSection = useCallback(async (target: WorkspaceTab) => {
    if (!topicId || target === "dashboard") return;
    setSectionStates((current) => ({
      ...current,
      [target]: current[target].phase === "error" ? retryResource(current[target]) : startResource(current[target]),
    }));
    try {
      if (target === "papers") {
        await loadPaperResources();
        return;
      }
      if (target === "insights") {
        setRecommendations(await getApiTopicRecommendations(topicId));
      }
      if (target === "reports") {
        const [reportResult, scheduleResult] = await Promise.allSettled([
          getApiReports({ topicId, limit: 50 }),
          getTopicSchedules(),
        ]);
        if (reportResult.status === "rejected") throw reportResult.reason;
        setReports(reportResult.value);
        if (scheduleResult.status === "fulfilled") setSchedules(scheduleResult.value.items);
        else {
          setSectionStates((current) => ({ ...current, reports: unavailableResource(current.reports, "보고서는 표시하지만 일정 서비스는 사용할 수 없습니다.") }));
          return;
        }
      }
      if (target === "settings") {
        const [providerResult, statusResult, scheduleResult] = await Promise.allSettled([
          getSourceProviders(),
          getSettingsStatus(),
          getTopicSchedules(),
        ]);
        if (providerResult.status === "fulfilled") setProviders(providerResult.value);
        if (statusResult.status === "fulfilled") setSettingsStatus(statusResult.value);
        if (scheduleResult.status === "fulfilled") setSchedules(scheduleResult.value.items);
        const failed = [providerResult, statusResult, scheduleResult].filter((result) => result.status === "rejected").length;
        if (failed) {
          setSectionStates((current) => ({ ...current, settings: unavailableResource(current.settings, `${failed}개 설정 리소스를 사용할 수 없습니다. 지원되는 설정만 표시합니다.`) }));
          return;
        }
      }
      setSectionStates((current) => ({ ...current, [target]: resolveResource(current[target], true) }));
    } catch (nextError) {
      if (target === "insights") setRecommendations(null);
      setSectionStates((current) => ({ ...current, [target]: failResource(current[target], nextError) }));
    }
  }, [loadPaperResources, topicId]);

  useEffect(() => {
    if (!topicId || tab === "dashboard") return;
    void loadSection(tab);
  }, [loadSection, tab, topicId]);

  useEffect(() => {
    if (tab !== "settings" || !profile?.profile_id) {
      setNotificationSettings(null);
      setNotificationState(createResourceState());
      return;
    }
    let cancelled = false;
    setNotificationState((current) => startResource(current));
    void getTopicNotificationSettings(profile.profile_id)
      .then((payload) => {
        if (!cancelled) {
          setNotificationSettings(payload);
          setNotificationState((current) => resolveResource(current, payload));
        }
      })
      .catch((nextError) => {
        if (!cancelled) setNotificationState((current) => failResource(current, nextError));
      });
    return () => { cancelled = true; };
  }, [profile?.profile_id, tab]);

  if (!rawTab || !VALID_TABS.has(rawTab)) {
    return <V2CompatibilityRoute />;
  }

  async function runAction(action: Exclude<TopicAction, "full_pipeline">) {
    if (busyActionRef.current) return;
    busyActionRef.current = action;
    setBusyAction(action);
    try {
      const result = await runApiTopicAction(topicId, action);
      setNotice(`${result.message} · run ${result.run_id}`);
      await loadBase();
      if (tab === "reports") setReports(await getApiReports({ topicId, limit: 50 }));
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "연구 작업을 시작하지 못했습니다."));
    } finally {
      busyActionRef.current = null;
      setBusyAction(null);
    }
  }

  async function toggleTask(task: ApiScheduledTask, enabled: boolean) {
    setBusyTaskId(task.id);
    try {
      await updateApiTask(task.id, { enabled });
      await loadBase();
      setNotice(`${task.title} 작업을 ${enabled ? "활성화" : "비활성화"}했습니다.`);
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "예약 작업 상태를 변경하지 못했습니다."));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function runTask(task: ApiScheduledTask) {
    setBusyTaskId(task.id);
    try {
      const result = await runApiTaskNow(task.id);
      setNotice(`예약 작업을 시작했습니다: ${result.run.run_id}`);
      await loadBase();
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "예약 작업을 실행하지 못했습니다."));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function toggleSchedule(schedule: TopicSchedule, enabled: boolean) {
    setBusyScheduleId(schedule.schedule_id);
    try {
      const response = await updateTopicSchedule(schedule.schedule_id, { enabled });
      setSchedules(response.items);
      setNotice(`${schedule.title} 일정을 ${enabled ? "활성화" : "비활성화"}했습니다.`);
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "보고서 일정을 변경하지 못했습니다."));
    } finally {
      setBusyScheduleId(null);
    }
  }

  async function toggleFavorite() {
    if (!profile) return;
    try {
      const response = await updateTopicProfile(profile.profile_id, { is_favorite: !profile.is_favorite });
      setProfiles(response.items);
      setNotice(profile.is_favorite ? "즐겨찾기에서 제거했습니다." : "즐겨찾기에 추가했습니다.");
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "즐겨찾기 상태를 저장하지 못했습니다."));
    }
  }

  async function saveSettings(payload: V2SettingsSavePayload) {
    setSettingsBusy(true);
    try {
      if (profile) {
        const response = await updateTopicProfile(profile.profile_id, {
          title: payload.title,
          description: payload.description || null,
          queries: payload.queries,
          default_collection_cadence: payload.collectionCadence,
          default_report_cadence: payload.reportCadence,
          acquisition_language: payload.acquisitionLanguage,
          review_report_language: payload.reviewReportLanguage,
          acquisition_start_date: payload.acquisitionStartDate,
          acquisition_end_date: payload.acquisitionEndDate,
          ...(payload.sourceSettings ? { source_settings: payload.sourceSettings } : {}),
        });
        setProfiles(response.items);
      }
      await saveTopicConfiguration(topicId, {
        displayName: payload.title,
        queryTerms: payload.queries,
        minRelevance: payload.minRelevance,
        acquisitionLanguage: payload.acquisitionLanguage,
        reviewReportLanguage: payload.reviewReportLanguage,
        acquisitionStartDate: payload.acquisitionStartDate,
        acquisitionEndDate: payload.acquisitionEndDate,
      });
      if (profile && payload.notification) {
        const response = await updateTopicNotificationSettings(profile.profile_id, {
          in_app_enabled: payload.notification.inAppEnabled,
          rules: payload.notification.rules,
          channels: payload.notification.channels,
        });
        setNotificationSettings(response);
      }
      setNotice("주제 설정을 저장했습니다. 연구 작업은 실행하지 않았습니다.");
      await loadBase();
    } catch (nextError) {
      setActionError(getErrorMessage(nextError, "주제 설정을 저장하지 못했습니다."));
      throw nextError;
    } finally {
      setSettingsBusy(false);
    }
  }

  function setPaperSearch(nextQuery: string) {
    const next = new URLSearchParams(searchParams);
    if (nextQuery) next.set("query", nextQuery); else next.delete("query");
    next.delete("page");
    setSearchParams(next, { replace: true });
  }

  function setPaperProvider(nextProvider: string) {
    const next = new URLSearchParams(searchParams);
    if (nextProvider) next.set("provider", nextProvider); else next.delete("provider");
    next.delete("page");
    setSearchParams(next, { replace: true });
  }

  function setPaperParameter(key: string, value: string, resetPage = true, replace = true) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value); else next.delete(key);
    if (resetPage) next.delete("page");
    setSearchParams(next, { replace });
  }

  function setPaperCollectionFilter(nextCollectionId: string) {
    setPaperParameter("collection", collectionIdToRouteValue(nextCollectionId), true, false);
  }

  function openPaperDetail(paperVersionId: string) {
    const next = new URLSearchParams(searchParams);
    next.set("paper", paperVersionId);
    next.delete("evidence");
    setSearchParams(next, { replace: true });
  }

  function closePaperDetail() {
    const next = new URLSearchParams(searchParams);
    next.delete("paper");
    next.delete("evidence");
    setSearchParams(next, { replace: true });
  }

  async function refreshPaperResources(collectionId = paperCollection) {
    return loadPaperResources(collectionId);
  }

  async function refreshPaperCollections() {
    setCollectionState((current) => current.phase === "error" ? retryResource(current) : startResource(current));
    try {
      const nextCollections = await getPaperCollections(topicId);
      setPaperCollections(nextCollections);
      setCollectionState((current) => resolveResource(current, nextCollections));
    } catch (nextError) {
      setCollectionState((current) => unavailableResource(current, getErrorMessage(nextError, "paper_collection_store_unavailable")));
    }
  }

  async function refreshAfterPaperMutation(collectionId = paperCollection) {
    try {
      const refreshed = await refreshPaperResources(collectionId);
      if (!refreshed.committed || !refreshed.bankSucceeded) throw new Error(refreshed.failureReason ?? "paper_bank_refresh_failed");
    } catch (nextError) {
      const message = getErrorMessage(nextError, "변경은 저장되었지만 최신 논문 은행 상태를 다시 불러오지 못했습니다.");
      setSectionStates((current) => ({ ...current, papers: failResource(current.papers, new Error(message)) }));
      setNotice("변경은 저장되었습니다. 최신 상태 새로고침이 필요합니다.");
    }
  }

  async function savePaperToCollection(paperVersionId: string, collectionId: string) {
    await addPaperToCollection(topicId, collectionId, paperVersionId);
    await refreshAfterPaperMutation();
  }

  async function removePaperFromNamedCollection(paperVersionId: string, collectionId: string) {
    await removePaperFromCollection(topicId, collectionId, paperVersionId);
    await refreshAfterPaperMutation();
  }

  async function bulkSavePapers(paperVersionIds: string[], collectionId: string): Promise<PaperBulkSaveResult> {
    const result = await bulkSavePapersToCollection(topicId, collectionId, paperVersionIds);
    await refreshAfterPaperMutation();
    return result;
  }

  async function exportPapers(query: { q?: string; provider?: string; collectionId?: string; year?: string; reviewState?: string; openAccess?: string; sort?: string; page?: number; pageSize?: number }): Promise<PaperBankExport> {
    return exportTopicPaperBank(topicId, query);
  }

  async function createNamedCollection(name: string) {
    await createPaperCollection(topicId, name);
    await refreshAfterPaperMutation();
  }

  async function renameNamedCollection(collectionId: string, name: string) {
    await renamePaperCollection(topicId, collectionId, name);
    await refreshAfterPaperMutation();
  }

  async function deleteNamedCollection(collectionId: string) {
    await deletePaperCollection(topicId, collectionId);
    const nextCollectionId = paperCollection === collectionId ? "" : paperCollection;
    if (paperCollection === collectionId) setPaperParameter("collection", "", true, false);
    await refreshAfterPaperMutation(nextCollectionId);
  }

  async function refreshActiveResource() {
    if (tab !== "papers") {
      if (tab === "dashboard") await loadBase();
      else await loadSection(tab);
      return;
    }
    if (paperRefreshBusyRef.current) return;
    paperRefreshBusyRef.current = true;
    const refreshGeneration = paperRequestGenerationRef.current + 1;
    setPaperRefreshState((current) => ({
      ...current,
      phase: "refreshing",
      generation: refreshGeneration,
      failureReason: null,
    }));
    try {
      const result = await loadPaperResources();
      if (!result.committed) return;
      if (result.bankSucceeded && result.failureReason === null) {
        setPaperRefreshState({
          supported: true,
          phase: "succeeded",
          generation: result.generation,
          failureReason: null,
          lastSuccessfulAt: new Date().toISOString(),
        });
      } else {
        setPaperRefreshState((current) => ({
          ...current,
          phase: "failed",
          generation: result.generation,
          failureReason: result.failureReason ?? "paper_resource_refresh_failed",
        }));
      }
    } finally {
      paperRefreshBusyRef.current = false;
    }
  }

  function resolveReportUrl(downloadUrl?: string | null) {
    if (!downloadUrl) return null;
    if (/^https?:\/\//i.test(downloadUrl)) return downloadUrl;
    return `${getApiBase()}${downloadUrl.startsWith("/") ? "" : "/"}${downloadUrl}`;
  }

  const topicName = dashboard?.topic?.name || profile?.title || topicId;
  const lastUpdated = dashboard?.topic?.updatedAt || profile?.updated_at || null;
  const sectionState = sectionStates[tab];
  const baseBusy = baseState.phase === "loading" || baseState.phase === "retrying";
  const sectionBusy = sectionState.phase === "loading" || sectionState.phase === "retrying";
  const papersPending = tab === "papers" && !paperBank && (sectionState.phase === "idle" || sectionBusy);
  const activeRefreshBusy = tab === "papers" ? paperRefreshState.phase === "refreshing" : tab === "dashboard" ? baseBusy : sectionBusy;
  const activeCollectionLabel = paperCollectionLabel(paperRoute.collection, paperCollections?.items);
  // Settings controls are rendered after the topic and settings resources settle. Expose
  // that state explicitly so browser assertions wait on the product boundary rather than a
  // timing guess or a transient shell mount.
  const settingsReady = tab === "settings"
    && Boolean(dashboard)
    && !baseBusy
    && !sectionBusy
    && sectionState.phase !== "error";

  return (
    <div
      className="v2-topic-workspace"
      data-settings-route={tab === "settings" ? "settings" : undefined}
      data-settings-ready={tab === "settings" ? String(settingsReady) : undefined}
    >
      <header className="v2-topic-header">
        <div className="v2-topic-title-row">
          <h1>{topicName}</h1>
          <button type="button" className={`v2-favorite-button${profile?.is_favorite ? " is-active" : ""}`} aria-label="즐겨찾기" disabled={!profile} onClick={() => void toggleFavorite()}><Star size={21} weight={profile?.is_favorite ? "fill" : "regular"} /></button>
        </div>
        <div className="v2-topic-meta">
          <V2Button className="v2-refresh-button" busy={activeRefreshBusy} aria-busy={activeRefreshBusy} onClick={() => void refreshActiveResource()}><ArrowClockwise size={14} />최근 업데이트: {formatUpdatedAt(lastUpdated, preferences.timeFormat ?? "locale")} · 새로고침</V2Button>
          <V2Button onClick={() => navigate(`/topics/${encodeURIComponent(topicId)}/settings`)}><Gear size={15} />주제 설정</V2Button>
        </div>
        <nav className="v2-topic-tabs" aria-label="주제 화면">
          <NavLink to={`/topics/${encodeURIComponent(topicId)}/dashboard`}>대시보드</NavLink>
          <NavLink to={`/topics/${encodeURIComponent(topicId)}/papers`}>논문 목록</NavLink>
          <NavLink to={`/topics/${encodeURIComponent(topicId)}/insights`}>분석 인사이트</NavLink>
          <NavLink to={`/topics/${encodeURIComponent(topicId)}/reports`}>보고서</NavLink>
          <NavLink to={`/topics/${encodeURIComponent(topicId)}/settings`}>설정</NavLink>
        </nav>
      </header>

      {notice ? <p className="v2-inline-notice" role="status">{notice}</p> : null}
      {paperRefreshState.phase === "succeeded" && tab === "papers" ? <p className="v2-inline-notice" role="status">논문 리소스를 새로고침했습니다.</p> : null}
      {paperRefreshState.phase === "failed" && tab === "papers" ? <p className="v2-inline-notice is-warning" role="alert">{paperRefreshState.failureReason}</p> : null}
      {actionError ? <V2Error title="작업을 완료하지 못했습니다." message={actionError} onDismiss={() => setActionError(null)} /> : null}
      {baseBusy && tab !== "papers" ? <V2Loading label={baseState.stale ? "기존 주제 데이터를 유지하며 다시 불러오는 중입니다." : "주제 데이터를 불러오는 중입니다."} /> : null}
      {baseState.phase === "error" && tab !== "papers" ? <V2Error message={baseState.message ?? "주제 대시보드를 불러오지 못했습니다."} onRetry={() => void loadBase()} /> : null}
      {profileState.phase === "error" && tab !== "papers" ? <V2Unavailable title="주제 프로필을 사용할 수 없습니다." reason={profileState.message ?? "프로필 API 오류"} onRetry={() => void loadBase()} /> : null}
      {sectionBusy && !baseBusy && tab !== "papers" ? <V2Loading label={sectionState.stale ? "기존 화면을 유지하며 다시 불러오는 중입니다." : "화면 데이터를 정리하는 중입니다."} /> : null}
      {sectionState.phase === "error" && tab !== "dashboard" && tab !== "papers" ? <V2Error message={sectionState.message ?? "화면 데이터를 불러오지 못했습니다."} onRetry={() => void loadSection(tab)} /> : null}
      {sectionState.phase === "unavailable" && tab !== "papers" ? <V2Unavailable title="일부 기능을 사용할 수 없습니다." reason={sectionState.message ?? "현재 서비스가 준비되지 않았습니다."} onRetry={() => void loadSection(tab)} /> : null}
      {tab === "settings" && notificationState.phase === "error" ? <V2Unavailable title="알림 설정을 사용할 수 없습니다." reason={notificationState.message ?? "알림 설정 API 오류"} /> : null}

      {tab === "papers" ? (
        <section className="v2-papers-route" aria-label="논문 목록 화면">
          {papersPending ? <V2Loading label={sectionState.stale ? "기존 논문 목록을 유지하며 다시 불러오는 중입니다." : "논문 목록을 불러오는 중입니다."} /> : null}
          {paperCollectionRouteAvailability === "missing" ? <div className="v2-state-card is-unavailable" role="status"><strong>요청한 컬렉션을 찾을 수 없습니다.</strong><p>{paperRoute.collection.routeValue}</p><V2Button aria-label="컬렉션 필터 지우기" onClick={() => setPaperCollectionFilter("")}>컬렉션 필터 지우기</V2Button></div> : null}
          {paperCollectionRouteAvailability === "malformed" ? <div className="v2-state-card is-unavailable" role="status"><strong>컬렉션 경로가 올바르지 않습니다.</strong><p>{paperRoute.collection.routeValue}</p><V2Button aria-label="컬렉션 필터 지우기" onClick={() => setPaperCollectionFilter("")}>컬렉션 필터 지우기</V2Button></div> : null}
          {sectionState.phase === "error" && paperCollectionRouteAvailability !== "missing" ? <V2Error message={sectionState.message ?? "논문 목록을 불러오지 못했습니다."} onRetry={() => void loadSection("papers")} /> : null}
          {sectionState.phase === "unavailable" && paperCollectionRouteAvailability !== "missing" && paperCollectionRouteAvailability !== "malformed" ? <V2Unavailable title="논문 목록을 사용할 수 없습니다." reason={sectionState.message ?? "Paper Bank 서비스가 준비되지 않았습니다."} onRetry={() => void loadSection("papers")} /> : null}
          {paperBank?.topicId === topicId && paperCollectionRouteAvailability !== "missing" && paperCollectionRouteAvailability !== "malformed" ? <V2PapersView
            paperBank={paperBank}
            collections={paperCollections}
            collectionsState={collectionState.phase === "unavailable" || collectionState.phase === "error" ? "unavailable" : paperCollections?.items.length ? "available" : "empty"}
            collectionsReason={collectionState.message}
            query={paperQuery}
            provider={paperProvider}
            collectionId={paperCollection}
            collectionLabel={activeCollectionLabel}
            collectionRouteKind={paperRoute.collection.kind}
            saveTargetCollectionId={saveTargetCollectionId}
            reviewState={paperReviewState}
            year={paperYear}
            openAccess={paperOpenAccess}
            sort={paperSort}
            openPaperVersionId={openPaperVersionId}
            openEvidenceId={openEvidenceId}
            onSearch={setPaperSearch}
            onProviderChange={setPaperProvider}
            onCollectionChange={setPaperCollectionFilter}
            onSaveTargetCollectionChange={setSaveTargetCollectionId}
            onReviewStateChange={(value) => setPaperParameter("review", value)}
            onYearChange={(value) => setPaperParameter("year", value)}
            onOpenAccessChange={(value) => setPaperParameter("openAccess", value)}
            onSortChange={(value) => setPaperParameter("sort", value)}
            onPageChange={(value) => setPaperParameter("page", String(value), false)}
            onOpenPaper={openPaperDetail}
            onClosePaper={closePaperDetail}
            onSavePaper={savePaperToCollection}
            onRemovePaper={removePaperFromNamedCollection}
            onBulkSave={bulkSavePapers}
            onExport={(query) => exportPapers(query)}
            onCreateCollection={createNamedCollection}
            onRenameCollection={renameNamedCollection}
            onDeleteCollection={deleteNamedCollection}
            onRetryCollections={() => void refreshPaperCollections()}
            onCollectionImported={() => refreshAfterPaperMutation()}
            onFreezeSelection={freezeSelectedPapers}
          /> : null}
        </section>
      ) : null}

      {tab === "insights" ? <TemporalResearchPanel key={`history-${topicId}`} topicId={topicId} /> : null}
      {tab === "reports" ? <JournalLibraryPanel topicId={topicId}/> : null}
      {tab === "reports" ? <LocalSynthesisPanel key={topicId} topicId={topicId} initialJobId={searchParams.get("synthesis") || undefined} onSelectJob={id => { const next = new URLSearchParams(searchParams); next.set("synthesis", id); setSearchParams(next, { replace: true }); }} /> : null}
      {!baseBusy && dashboard ? (
        <>
          {tab === "dashboard" ? <V2DashboardView dashboard={dashboard} busyAction={busyAction} busyTaskId={busyTaskId} onRunAction={(action) => void runAction(action)} onToggleTask={(task, enabled) => void toggleTask(task, enabled)} onRunTask={(task) => void runTask(task)} onOpenReports={() => navigate(`/topics/${encodeURIComponent(topicId)}/reports`)} onOpenPapers={() => navigate(`/topics/${encodeURIComponent(topicId)}/papers`)} onOpenSettings={() => navigate(`/topics/${encodeURIComponent(topicId)}/settings`)} onRetrySchedules={() => void loadBase()} /> : null}
          {tab === "insights" && !sectionBusy ? <V2InsightsView topicId={topicId} dashboard={dashboard} recommendations={recommendations} onRecommendationsChange={data => {setRecommendations(data);setSectionStates(current=>({...current,insights:resolveResource(current.insights,true)}));}} busyAction={busyAction} onRunAction={(action) => void runAction(action)} /> : null}
          {tab === "reports" && reports && !sectionBusy ? <V2ReportsView key={topicId} reports={reports} schedules={topicSchedules} busyAction={busyAction} busyScheduleId={busyScheduleId} onGenerateReport={() => void runAction("report")} onToggleSchedule={(schedule, enabled) => void toggleSchedule(schedule, enabled)} resolveReportUrl={resolveReportUrl} /> : null}
          {tab === "settings" && !sectionBusy && sectionState.phase !== "error" ? <V2SettingsView topicId={topicId} dashboard={dashboard} profile={profile} providers={providers} notificationSettings={notificationSettings} schedules={topicSchedules} settingsStatus={settingsStatus} busy={settingsBusy} onSave={saveSettings} /> : null}
        </>
      ) : null}
    </div>
  );
}

export function V2DefaultRoute({ tab = "dashboard", query = "" }: { tab?: string; query?: string }) {
  const location = useLocation();
  const [topicId, setTopicId] = useState<string | null>(null);
  const [phase, setPhase] = useState<"loading" | "empty" | "unavailable" | "error">("loading");
  const [retryKey, setRetryKey] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setPhase("loading");
    void Promise.allSettled([getApiTopics(), getTopicProfiles()]).then(([apiResult, profileResult]) => {
      if (cancelled) return;
      const apiTopics = apiResult.status === "fulfilled" ? apiResult.value.items.filter((item) => item.status === "active") : [];
      const profiles = profileResult.status === "fulfilled" ? profileResult.value.items.filter((item) => item.status === "active") : [];
      const preferredApi = apiTopics.find((item) => item.name === "Large Language Models") ?? apiTopics[0];
      const preferredProfile = profiles.find((item) => item.title === "Large Language Models") ?? profiles[0];
      const id = preferredApi?.id ?? preferredProfile?.profile_id ?? null;
      if (id) {
        setTopicId(id);
        return;
      }
      const failureCount = [apiResult, profileResult].filter((result) => result.status === "rejected").length;
      setPhase(failureCount === 2 ? "error" : failureCount === 1 ? "unavailable" : "empty");
    });
    return () => { cancelled = true; };
  }, [retryKey]);
  const preservedSearch = query || location.search;
  if (topicId) return <Navigate replace to={`/topics/${encodeURIComponent(topicId)}/${tab}${preservedSearch}`} />;
  if (phase === "error") return <V2Error message="주제 색인과 프로필 API를 모두 불러오지 못했습니다." onRetry={() => setRetryKey((value) => value + 1)} />;
  if (phase === "unavailable") return <V2Unavailable title="기본 주제를 결정할 수 없습니다." reason="주제 색인 또는 프로필 API 중 하나가 실패했고 나머지 응답은 비어 있습니다." onRetry={() => setRetryKey((value) => value + 1)} />;
  if (phase === "empty") return <div className="v2-state-card"><strong>등록된 연구 주제가 없습니다.</strong><p>왼쪽의 새 주제 추가 버튼으로 첫 주제를 만드세요.</p></div>;
  return <V2Loading label="기본 연구 주제를 찾는 중입니다." />;
}

export function V2TopicIndexRedirect() {
  const { topicId } = useParams<{ topicId: string }>();
  return <Navigate replace to={`/topics/${encodeURIComponent(topicId ?? "")}/dashboard`} />;
}
