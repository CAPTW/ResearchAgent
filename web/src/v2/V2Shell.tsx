import { FormEvent, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  Bell,
  BookBookmark,
  Brain,
  CaretDown,
  CheckCircle,
  Circuitry,
  FileText,
  FirstAid,
  Gear,
  Graph,
  MagnifyingGlass,
  Plus,
  Question,
  Robot,
  SlidersHorizontal,
  Sparkle,
  SpinnerGap,
  Star,
  X,
} from "@phosphor-icons/react";

import {
  createTopicProfile,
  getApiTopics,
  getTopicProfiles,
  type GlobalSearchItem,
  type AuthSessionResponse,
} from "../lib/api";
import * as api from "../lib/api";
import { useOptionalAppearance } from "./AppearanceProvider";
import { formatTimestamp } from "./appearance";
import { NativeDownloadNotice } from "./components/NativeDownloadNotice";

type V2ShellProps = {
  auth: AuthSessionResponse;
  children: ReactNode;
  onLogout: () => Promise<void>;
};

type ShellTopic = {
  id: string;
  name: string;
  description: string;
  updatedAt: string | null;
  favorite: boolean;
};

const TOPIC_ICONS = [Brain, Circuitry, FirstAid, Robot, Sparkle, Graph];

function getActiveTopicId(pathname: string) {
  const match = pathname.match(/\/topics\/([^/]+)/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function formatRelativeUpdate(value: string | null) {
  if (!value) return "업데이트 기록 없음";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return value;
  const hours = Math.max(1, Math.round((Date.now() - timestamp) / 3_600_000));
  if (hours < 24) return `최근 업데이트: ${hours}시간 전`;
  const days = Math.max(1, Math.round(hours / 24));
  return `최근 업데이트: ${days}일 전`;
}

function mergeTopics(apiTopics: ShellTopic[], profiles: ShellTopic[]) {
  const merged = new Map<string, ShellTopic>();
  [...apiTopics, ...profiles].forEach((topic) => {
    const existing = merged.get(topic.id);
    merged.set(topic.id, existing ? { ...topic, ...existing } : topic);
  });
  return [...merged.values()];
}

function NewTopicDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (topicId: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [queries, setQueries] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) {
      setTitle("");
      setDescription("");
      setQueries("");
      setError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    titleRef.current?.focus();
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [busy, onClose, open]);

  if (!open) return null;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedTitle = title.trim();
    if (!trimmedTitle) return;
    setBusy(true);
    setError(null);
    try {
      const response = await createTopicProfile({
        title: trimmedTitle,
        description: description.trim() || null,
        topic: trimmedTitle,
        queries: queries.split(",").map((item) => item.trim()).filter(Boolean),
        categories: [],
        max_results: 100,
        top_papers: 20,
        weekly_top: 10,
        is_favorite: true,
        default_collection_cadence: "manual",
        default_report_cadence: "weekly",
      });
      const created = response.items.find((item) => item.title === trimmedTitle || item.topic === trimmedTitle);
      if (!created) throw new Error("생성된 주제 ID를 확인하지 못했습니다.");
      onCreated(created.profile_id);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "주제를 생성하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="v2-modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="v2-modal" role="dialog" aria-modal="true" aria-labelledby="v2-new-topic-title">
        <header>
          <div>
            <span className="v2-eyebrow">Research topic</span>
            <h2 id="v2-new-topic-title">새 연구 주제 추가</h2>
          </div>
          <button type="button" className="v2-icon-button" aria-label="닫기" onClick={onClose}><X size={18} /></button>
        </header>
        <form onSubmit={handleSubmit}>
          <label>주제 이름<input ref={titleRef} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="예: Spatial Computing Agents" /></label>
          <label>설명<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="이 주제에서 추적할 문제와 범위를 입력하세요." /></label>
          <label>검색 키워드<input value={queries} onChange={(event) => setQueries(event.target.value)} placeholder="agent, spatial computing, evaluation" /></label>
          {error ? <p className="v2-form-error" role="alert">{error}</p> : null}
          <footer>
            <button type="button" className="v2-button" onClick={onClose}>취소</button>
            <button type="submit" className="v2-button is-primary" disabled={busy || !title.trim()}>
              {busy ? <SpinnerGap className="v2-spin" size={16} /> : <Plus size={16} />} 주제 만들기
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

export function V2Shell({ auth, children, onLogout }: V2ShellProps) {
  const { preferences } = useOptionalAppearance();
  const location = useLocation();
  const navigate = useNavigate();
  const [topics, setTopics] = useState<ShellTopic[]>([]);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchResults, setSearchResults] = useState<GlobalSearchItem[]>([]);
  const [searchState, setSearchState] = useState<"idle" | "loading" | "ready" | "empty" | "error">("idle");
  const [searchUnavailable, setSearchUnavailable] = useState<string[]>([]);
  const [searchSelectedIndex, setSearchSelectedIndex] = useState(0);
  const searchGeneration = useRef(0);
  const priorSearchFocus = useRef<HTMLElement | null>(null);
  const [newTopicOpen, setNewTopicOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loadingTopics, setLoadingTopics] = useState(true);
  const [topicLoadErrors, setTopicLoadErrors] = useState<string[]>([]);
  const [showAllTopics, setShowAllTopics] = useState(false);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const newTopicButtonRef = useRef<HTMLButtonElement | null>(null);

  const loadTopics = async () => {
    setLoadingTopics(true);
    const [apiResult, profileResult] = await Promise.allSettled([getApiTopics(), getTopicProfiles()]);
    const loadErrors = [
      apiResult.status === "rejected" ? "주제 색인을 불러오지 못했습니다." : null,
      profileResult.status === "rejected" ? "주제 프로필을 불러오지 못했습니다." : null,
    ].filter((value): value is string => Boolean(value));
    const apiTopics: ShellTopic[] = apiResult.status === "fulfilled"
      ? apiResult.value.items.filter((topic) => topic.status === "active").map((topic) => ({
          id: topic.id,
          name: topic.name,
          description: topic.description ?? "",
          updatedAt: topic.updatedAt,
          favorite: topic.isFavorite,
        }))
      : [];
    const profiles: ShellTopic[] = profileResult.status === "fulfilled"
      ? profileResult.value.items.filter((profile) => profile.status === "active").map((profile) => ({
          id: profile.profile_id,
          name: profile.title || profile.topic,
          description: profile.description ?? "",
          updatedAt: profile.updated_at,
          favorite: profile.is_favorite,
        }))
      : [];
    setTopics(mergeTopics(apiTopics, profiles));
    setTopicLoadErrors(loadErrors);
    setLoadingTopics(false);
  };

  useEffect(() => {
    void loadTopics();
  }, []);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        priorSearchFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setSearchOpen(true);
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);

  useEffect(() => {
    const query = search.trim();
    if (!searchOpen || !query) {
      setSearchResults([]);
      setSearchUnavailable([]);
      setSearchState(searchOpen ? "idle" : "idle");
      return;
    }
    const searchGlobalFn = Object.prototype.hasOwnProperty.call(api, "searchGlobal") ? api.searchGlobal : undefined;
    if (typeof searchGlobalFn !== "function") {
      setSearchState("error");
      return;
    }
    const generation = ++searchGeneration.current;
    const controller = new AbortController();
    setSearchState("loading");
    const timer = window.setTimeout(() => {
      void searchGlobalFn({ q: query, limit: 12 }, { signal: controller.signal })
        .then((response) => {
          if (generation !== searchGeneration.current) return;
          setSearchResults(response.items);
          setSearchUnavailable(Object.values(response.unavailableKinds ?? {}));
          setSearchSelectedIndex(0);
          setSearchState(response.items.length ? "ready" : "empty");
        })
        .catch((error) => {
          if (controller.signal.aborted || generation !== searchGeneration.current) return;
          setSearchResults([]);
          setSearchState("error");
          setSearchUnavailable([error instanceof Error ? error.message : "검색을 불러오지 못했습니다."]);
        });
    }, 120);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [search, searchOpen]);

  const activeTopicId = getActiveTopicId(location.pathname) ?? topics[0]?.id ?? null;
  const savedPapersPath = activeTopicId
    ? `/topics/${encodeURIComponent(activeTopicId)}/papers?collection=saved`
    : null;
  const savedPapersActive = Boolean(
    activeTopicId
      && location.pathname === `/topics/${encodeURIComponent(activeTopicId)}/papers`
      && new URLSearchParams(location.search).get("collection") === "saved",
  );
  const filteredTopics = useMemo(() => {
    const term = search.trim().toLocaleLowerCase("ko-KR");
    if (!term) return topics;
    return topics.filter((topic) => `${topic.name} ${topic.description}`.toLocaleLowerCase("ko-KR").includes(term));
  }, [search, topics]);

  function topicPath(tab: string) {
    return activeTopicId ? `/topics/${encodeURIComponent(activeTopicId)}/${tab}` : "/";
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const selected = searchResults[searchSelectedIndex];
    if (selected && selected.route.startsWith("/") && !selected.route.startsWith("//")) {
      navigate(selected.route);
      setSearchOpen(false);
      return;
    }
    const match = filteredTopics[0];
    if (match) {
      navigate(`/topics/${encodeURIComponent(match.id)}/papers?query=${encodeURIComponent(search.trim())}`);
      setSearchOpen(false);
    }
  }

  function closeSearchAndRestoreFocus() {
    setSearchOpen(false);
    setSearchSelectedIndex(0);
    const target = priorSearchFocus.current;
    if (target && target !== searchRef.current && document.contains(target)) {
      window.requestAnimationFrame(() => target.focus());
    }
  }

  function handleSearchKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeSearchAndRestoreFocus();
      return;
    }
    if (!searchResults.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSearchSelectedIndex((index) => (index + 1) % searchResults.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSearchSelectedIndex((index) => (index - 1 + searchResults.length) % searchResults.length);
    } else if (event.key === "Enter" && searchResults[searchSelectedIndex]) {
      event.preventDefault();
      submitSearch(event as unknown as FormEvent<HTMLFormElement>);
    }
  }

  const displayName = auth.user?.display_name || (auth.auth_mode === "disabled" ? "VK Tech" : "Research Operator");
  const initials = displayName.split(/\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "VK";

  return (
    <div className="v2-app-shell">
      <button className="v2-mobile-menu" type="button" aria-expanded={sidebarOpen} aria-controls="v2-sidebar" onClick={() => setSidebarOpen((value) => !value)}>
        {sidebarOpen ? <X size={20} /> : <Graph size={20} />}
        <span>메뉴</span>
      </button>

      <aside id="v2-sidebar" className={`v2-sidebar${sidebarOpen ? " is-open" : ""}`} aria-label="연구 주제 탐색">
        <NavLink className="v2-brand" to={topicPath("dashboard")} aria-label="AI Research Agent 대시보드">
          <span className="v2-brand-mark"><Graph size={31} weight="duotone" /></span>
          <span><strong>AI Research Agent</strong><small>Paper Intelligence Dashboard</small></span>
        </NavLink>

        <button ref={newTopicButtonRef} type="button" className="v2-new-topic" onClick={() => setNewTopicOpen(true)}><Plus size={17} weight="bold" />새 주제 추가</button>

        <section className="v2-sidebar-section v2-topic-section">
          <header><span>주제 목록</span><strong>{loadingTopics ? "…" : topics.length}</strong></header>
          <div className="v2-topic-list">
            {filteredTopics.slice(0, showAllTopics ? filteredTopics.length : 7).map((topic, index) => {
              const TopicIcon = TOPIC_ICONS[index % TOPIC_ICONS.length];
              return (
                <button
                  type="button"
                  key={topic.id}
                  className={topic.id === activeTopicId ? "is-active" : ""}
                  onClick={() => navigate(`/topics/${encodeURIComponent(topic.id)}/dashboard`)}
                >
                  <span className={`v2-topic-icon tone-${(index % 6) + 1}`}><TopicIcon size={14} weight="fill" /></span>
                  <span><strong>{topic.name}</strong><small>{formatRelativeUpdate(topic.updatedAt)}</small></span>
                  <Star size={14} weight={topic.favorite ? "fill" : "regular"} className={topic.favorite ? "is-favorite" : ""} />
                </button>
              );
            })}
            {!loadingTopics && filteredTopics.length === 0 ? <p className="v2-sidebar-empty">일치하는 주제가 없습니다.</p> : null}
            {topicLoadErrors.map((message) => <p key={message} className="v2-sidebar-error" role="alert">{message}</p>)}
          </div>
          {filteredTopics.length > 7 ? <button type="button" className="v2-sidebar-more" aria-expanded={showAllTopics} onClick={() => setShowAllTopics((value) => !value)}>{showAllTopics ? "접기" : "더보기"} <CaretDown size={12} /></button> : null}
          {topicLoadErrors.length ? <button type="button" className="v2-sidebar-more" onClick={() => void loadTopics()}>주제 다시 불러오기</button> : null}
        </section>

        <section className="v2-sidebar-section v2-resource-section">
          <header><span>리소스</span></header>
          {savedPapersPath ? (
            <Link className={savedPapersActive ? "active" : undefined} aria-current={savedPapersActive ? "page" : undefined} to={savedPapersPath}>
              <BookBookmark size={16} />저장된 논문
            </Link>
          ) : (
            <span className="v2-resource-disabled" aria-disabled="true" title="먼저 연구 주제를 선택하세요."><BookBookmark size={16} />저장된 논문</span>
          )}
          <NavLink to="/automations?view=templates"><FileText size={16} />보고서 템플릿</NavLink>
          <NavLink to="/settings/analysis?section=analysis"><SlidersHorizontal size={16} />분석 설정</NavLink>
          <NavLink to="/evidence?view=guide"><Question size={16} />사용 가이드</NavLink>
        </section>
      </aside>

      {sidebarOpen ? <button type="button" className="v2-sidebar-scrim" aria-label="메뉴 닫기" onClick={() => setSidebarOpen(false)} /> : null}

      <section className="v2-main-column">
        <header className="v2-topbar">
          <form className="v2-global-search" role="search" onSubmit={submitSearch}>
            <MagnifyingGlass size={18} />
            <input ref={searchRef} type="search" aria-label="주제, 키워드, 논문 검색" aria-expanded={searchOpen} aria-controls="v2-global-search-results" value={search} onMouseDown={() => { if (document.activeElement !== searchRef.current) priorSearchFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }} onFocus={() => setSearchOpen(true)} onChange={(event) => { setSearch(event.target.value); setSearchOpen(true); }} onKeyDown={handleSearchKeyDown} placeholder="주제, 키워드, 논문 검색..." />
            <kbd>Ctrl K</kbd>
            {searchOpen ? <section id="v2-global-search-results" className="v2-global-search-results" aria-label="전역 검색 결과">
              {searchState === "loading" ? <p role="status">검색 중…</p> : null}
              {searchState === "error" ? <p role="alert">검색 결과를 불러오지 못했습니다.</p> : null}
              {searchState === "empty" ? <p role="status">일치하는 결과가 없습니다.</p> : null}
              {searchUnavailable.map((message) => <p className="v2-search-unavailable" key={message}>일부 결과 종류를 사용할 수 없습니다: {message}</p>)}
              {searchResults.map((result, index) => <button type="button" className={index === searchSelectedIndex ? "is-selected" : undefined} key={result.resultId} aria-current={index === searchSelectedIndex ? "true" : undefined} onMouseDown={(event) => event.preventDefault()} onClick={() => { if (result.route.startsWith("/") && !result.route.startsWith("//")) { navigate(result.route); setSearchOpen(false); } }}><strong>{result.primaryLabel}</strong><small><span>{result.kind}</span> {result.secondaryText}{result.updatedAt ? ` · ${formatTimestamp(result.updatedAt, preferences.timeFormat ?? "locale", "ko-KR")}` : ""}</small></button>)}
              {searchResults.length ? <Link className="v2-search-help-link" to="/evidence?view=search-help" onClick={() => setSearchOpen(false)}>검색 도움말 보기</Link> : null}
            </section> : null}
          </form>
          <div className="v2-user-actions">
            <NavLink to={`${topicPath("settings")}?section=notifications`} aria-label="알림"><Bell size={19} /></NavLink>
            <NavLink to="/evidence?view=help" aria-label="도움말"><Question size={19} /></NavLink>
            <span className="v2-user-avatar">{initials}</span>
            <span className="v2-user-name">{displayName}</span>
            {auth.auth_mode === "oidc" ? <button type="button" onClick={() => void onLogout()}>로그아웃</button> : <span className="v2-local-mode-label">로컬 단일 사용자</span>}
          </div>
        </header>

        <main className="v2-main-content" data-route={location.pathname}>{children}</main>
      </section>

      <NewTopicDialog
        open={newTopicOpen}
        onClose={() => {
          setNewTopicOpen(false);
          window.requestAnimationFrame(() => newTopicButtonRef.current?.focus());
        }}
        onCreated={(topicId) => {
          setNewTopicOpen(false);
          void loadTopics();
          navigate(`/topics/${encodeURIComponent(topicId)}/dashboard`);
        }}
      />

      <NativeDownloadNotice />
      <div className="v2-session-state" aria-live="polite">
        <CheckCircle size={14} weight="fill" /> {auth.authenticated ? "연결됨" : "로그인 필요"}
        <Gear size={14} /> {auth.auth_mode === "disabled" ? "로컬 모드" : "인증 세션"}
      </div>
    </div>
  );
}
