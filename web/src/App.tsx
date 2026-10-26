import { useEffect, useMemo, useState } from "react";
import { Route, Routes } from "react-router-dom";

import { AuthSessionResponse, getApiBase, getAuthMe, logoutAuth } from "./lib/api";
import { buildLoginNavigationUrl, currentAppReturnTarget } from "./lib/authNavigation";
import { V2Shell } from "./v2/V2Shell";
import { JournalReviewDraftNotice, JournalReviewDraftProvider } from "./v2/JournalReviewDrafts";
import { SavedAnalysisDraftNotice, SavedAnalysisDraftProvider } from "./v2/SavedAnalysisDrafts";
import { V2CompatibilityRoute, V2EvidenceRoute } from "./v2/routeIntent";
import {
  V2DefaultRoute,
  V2TopicIndexRedirect,
  V2TopicWorkspaceScreen,
} from "./v2/V2TopicWorkspaceScreen";

export function App() {
  const [auth, setAuth] = useState<AuthSessionResponse | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const [authRevision, setAuthRevision] = useState(0);

  async function refreshAuth(nextNotice: string | null = null) {
    setAuthRevision(value => value + 1);
    setAuthLoading(true);
    try {
      const session = await getAuthMe();
      setAuth(session);
      setAuthError(null);
      setAuthNotice(nextNotice);
    } catch (error) {
      setAuth(null);
      setAuthError(error instanceof Error ? error.message : "인증 상태를 확인하지 못했습니다.");
    } finally {
      setAuthLoading(false);
    }
  }

  useEffect(() => {
    void refreshAuth();
  }, []);

  useEffect(() => {
    function handleUnauthorized() {
      void refreshAuth("세션이 만료되었습니다. 다시 로그인하세요.");
    }

    window.addEventListener("xrml:auth-unauthorized", handleUnauthorized);
    return () => window.removeEventListener("xrml:auth-unauthorized", handleUnauthorized);
  }, []);

  const authRequired = useMemo(() => auth?.auth_mode === "oidc" && !auth.authenticated, [auth]);
  const authConfigurationInvalid = auth?.session_configuration_status === "invalid";

  function handleLogin() {
    if (authConfigurationInvalid) {
      setAuthNotice("OIDC 세션 구성이 올바르지 않아 로그인할 수 없습니다.");
      return;
    }
    const loginUrl = auth?.login_url || `${getApiBase()}/auth/login`;
    const returnTarget = currentAppReturnTarget(window.location);
    window.location.assign(buildLoginNavigationUrl(loginUrl, returnTarget));
  }

  async function handleLogout() {
    await logoutAuth();
    await refreshAuth("로그아웃되었습니다.");
  }

  if (authLoading) {
    return <main className="xrml-app-boot" aria-busy="true">인증 상태를 확인하는 중입니다.</main>;
  }

  if (authError || !auth) {
    return <main className="xrml-app-boot is-error">{authError ?? "인증 상태를 확인하지 못했습니다."}</main>;
  }

  if (authRequired) {
    return (
      <main className="xrml-auth-gate">
        <section>
          <p className="v2-eyebrow">Authentication</p>
          <h1>로그인이 필요합니다</h1>
          <p>
            {authNotice ?? (
              authConfigurationInvalid
                ? "OIDC 세션 구성이 올바르지 않아 운영자가 설정을 수정해야 합니다."
                : "공유 배포에서는 비공개 실행을 열기 전에 제품 로그인이 필요합니다."
            )}
          </p>
          {!authConfigurationInvalid ? (
            <button className="v2-button is-primary" type="button" onClick={handleLogin}>로그인</button>
          ) : null}
        </section>
      </main>
    );
  }

  return (
    <JournalReviewDraftProvider key={`${authRevision}:${auth.auth_mode}:${auth.user?.user_id ?? "local"}`}>
    <SavedAnalysisDraftProvider>
    <V2Shell auth={auth} onLogout={handleLogout}>
      <JournalReviewDraftNotice/>
      <SavedAnalysisDraftNotice/>
      {auth.warnings?.length ? (
        <details className="v2-inline-notice is-warning" aria-label="인증 운영 경고">
          <summary>인증 운영 경고</summary>
          {auth.warnings.map((warning) => <span key={warning}>{warning}</span>)}
        </details>
      ) : null}
      <Routes>
        <Route path="/" element={<V2DefaultRoute />} />
        <Route path="/topics" element={<V2DefaultRoute />} />
        <Route path="/topics/:topicId" element={<V2TopicIndexRedirect />} />
        <Route path="/topics/:topicId/:tab" element={<V2TopicWorkspaceScreen />} />

        <Route path="/portfolio" element={<V2CompatibilityRoute />} />
        <Route path="/provenance" element={<V2CompatibilityRoute />} />
        <Route path="/evidence" element={<V2EvidenceRoute />} />
        <Route path="/new-run" element={<V2CompatibilityRoute />} />
        <Route path="/runs" element={<V2CompatibilityRoute />} />
        <Route path="/runs/:runId" element={<V2CompatibilityRoute />} />
        <Route path="/topics/:topicId/runs/:runId" element={<V2CompatibilityRoute />} />
        <Route path="/reports" element={<V2DefaultRoute tab="reports" />} />
        <Route path="/reports/:reportId" element={<V2CompatibilityRoute />} />
        <Route path="/recommendations" element={<V2CompatibilityRoute />} />
        <Route path="/compare" element={<V2CompatibilityRoute />} />
        <Route path="/automations" element={<V2CompatibilityRoute />} />
        <Route path="/settings" element={<V2DefaultRoute tab="settings" />} />
        <Route path="/settings/notifications" element={<V2CompatibilityRoute />} />
        <Route path="/settings/analysis" element={<V2CompatibilityRoute />} />
        <Route path="/project-bundle" element={<V2CompatibilityRoute />} />
        <Route path="/admin/legacy-runs" element={<V2CompatibilityRoute />} />
        <Route path="*" element={<V2CompatibilityRoute />} />
      </Routes>
    </V2Shell>
    </SavedAnalysisDraftProvider>
    </JournalReviewDraftProvider>
  );
}
