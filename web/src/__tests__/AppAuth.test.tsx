import { MemoryRouter, useLocation } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { reviewInput, useJournalReviewDraft } from "../v2/JournalReviewDrafts";

function DraftProbe() {
  const { draft, update } = useJournalReviewDraft("auth-test-candidate", { entries: [], note: "", base: null, baseline: reviewInput([], ""), busy: false, conflict: false, error: "", message: "", attempt: null, savedReviewId: null });
  return <input aria-label="Session review input" value={draft.note} onChange={event => { const note = event.target.value; update(d => ({ ...d, note })); }}/>;
}

function LocationProbe() {
  return <output aria-label="current route">{useLocation().pathname}</output>;
}

const apiMocks = vi.hoisted(() => ({
  getApiBase: vi.fn(() => "http://127.0.0.1:8000"),
  getAuthMe: vi.fn(),
  logoutAuth: vi.fn(),
  getApiTopics: vi.fn(),
  getTopicProfiles: vi.fn(),
  createTopicProfile: vi.fn(),
}));

vi.mock("../lib/api", () => apiMocks);
vi.mock("../v2/V2TopicWorkspaceScreen", () => ({
  V2DefaultRoute: ({ tab = "dashboard", query = "" }: { tab?: string; query?: string }) => (
    <div>{`Mock V2 Redirect: ${tab}${query}`}</div>
  ),
  V2TopicIndexRedirect: () => <div>Mock V2 Topic Redirect</div>,
  V2TopicWorkspaceScreen: () => <><div>Mock V2 Topic Workspace</div><DraftProbe/></>,
}));

describe("App auth integration", () => {
  beforeEach(() => {
    apiMocks.getApiBase.mockReturnValue("http://127.0.0.1:8000");
    apiMocks.getAuthMe.mockReset();
    apiMocks.logoutAuth.mockReset();
    apiMocks.getApiTopics.mockReset();
    apiMocks.getTopicProfiles.mockReset();
    apiMocks.createTopicProfile.mockReset();
    apiMocks.getApiTopics.mockResolvedValue({ items: [] });
    apiMocks.getTopicProfiles.mockResolvedValue({ items: [] });
  });

  it("clears temporary review input on authentication refresh even when the same actor returns immediately", async () => {
    apiMocks.getAuthMe.mockResolvedValue({ auth_mode: "oidc", authenticated: true, is_admin: false, user: { user_id: "same-actor", display_name: "Same Actor" }, warnings: [] });
    render(<MemoryRouter initialEntries={["/topics/topic-a/reports"]}><App/></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Session review input"), { target: { value: "Private unsaved review" } });
    expect(screen.getByLabelText("Session review input")).toHaveProperty("value", "Private unsaved review");
    await act(async () => { window.dispatchEvent(new Event("xrml:auth-unauthorized")); });
    expect(await screen.findByLabelText("Session review input")).toHaveProperty("value", "");
    expect(apiMocks.getAuthMe).toHaveBeenCalledTimes(2);
  });

  it("renders existing flows in auth-disabled mode", async () => {
    apiMocks.getAuthMe.mockResolvedValue({
      auth_mode: "disabled",
      authenticated: true,
      is_admin: true,
      user: {
        user_id: "local-internal-user",
        display_name: "Local Internal Mode",
        email: null,
        auth_source: "disabled",
      },
      login_url: null,
      logout_url: "http://127.0.0.1:8000/auth/logout",
      mcp_url: "http://127.0.0.1:8000/mcp",
      mcp_resource_metadata_url: "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp",
      product_auth_server_side_only: true,
      provider_auth_server_side_only: true,
      product_session_backend: "local_internal",
      warnings: ["AUTH_MODE=disabled is an explicit internal single-user mode."],
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Mock V2 Redirect: dashboard")).toBeTruthy();
    expect(screen.getByText("Local Internal Mode")).toBeTruthy();
    expect(screen.getByText("인증 운영 경고")).toBeTruthy();
  });

  it("keeps the app-base provenance route as an explicit compatibility state", async () => {
    apiMocks.getAuthMe.mockResolvedValue({
      auth_mode: "disabled",
      authenticated: true,
      is_admin: false,
      user: {
        user_id: "local-internal-user",
        display_name: "Local Internal Mode",
        email: null,
        auth_source: "disabled",
      },
      login_url: null,
      logout_url: "http://127.0.0.1:8000/auth/logout",
      mcp_url: null,
      mcp_resource_metadata_url: null,
      product_auth_server_side_only: true,
      provider_auth_server_side_only: true,
      product_session_backend: "local_internal",
      warnings: [],
    });

    render(
      <MemoryRouter basename="/app" initialEntries={["/app/provenance"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("status", { name: "호환 경로 상태" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "증거·provenance 작업공간" })).toBeTruthy();
    expect(screen.getByText(/식별자와 요청 의도만 안전하게 보존/)).toBeTruthy();
    const navigation = screen.getByRole("complementary", { name: "연구 주제 탐색" });
    expect(within(navigation).getByRole("link", { name: /AI Research Agent 대시보드/ })).toBeTruthy();
    expect(within(navigation).getByRole("link", { name: "사용 가이드" }).getAttribute("href")).toBe(
      "/app/evidence?view=guide",
    );
  });

  it("submits a matching topic from the global search", async () => {
    const user = userEvent.setup();
    apiMocks.getAuthMe.mockResolvedValue({
      auth_mode: "disabled",
      authenticated: true,
      is_admin: false,
      user: null,
      login_url: null,
      logout_url: "http://127.0.0.1:8000/auth/logout",
      mcp_url: null,
      mcp_resource_metadata_url: null,
      product_auth_server_side_only: true,
      provider_auth_server_side_only: true,
      product_session_backend: "local_internal",
      warnings: [],
    });
    apiMocks.getApiTopics.mockResolvedValue({
      items: [
        {
          id: "evidence-grounded-spatial-computing",
          name: "Evidence-grounded spatial computing systems",
          description: "A bounded spatial-computing evidence review.",
          updatedAt: "2026-08-25T00:00:00Z",
          status: "active",
          isFavorite: true,
        },
      ],
    });

    render(
      <MemoryRouter initialEntries={["/reports"]}>
        <App />
        <LocationProbe />
      </MemoryRouter>,
    );

    const search = await screen.findByRole("searchbox", { name: "주제, 키워드, 논문 검색" });
    await screen.findByRole("button", { name: /Evidence-grounded spatial computing systems/ });
    await user.type(search, "Evidence-grounded spatial computing systems{Enter}");

    await waitFor(() => {
      expect(screen.getByRole("status", { name: "current route" }).textContent).toBe(
        "/topics/evidence-grounded-spatial-computing/papers",
      );
    });
  });

  it("renders a protected-state login gate in oidc mode", async () => {
    apiMocks.getAuthMe.mockResolvedValue({
      auth_mode: "oidc",
      authenticated: false,
      is_admin: false,
      user: null,
      login_url: "http://127.0.0.1:8000/auth/login",
      logout_url: "http://127.0.0.1:8000/auth/logout",
      mcp_url: "http://127.0.0.1:8000/mcp",
      mcp_resource_metadata_url: "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp",
      product_auth_server_side_only: true,
      provider_auth_server_side_only: true,
      warnings: [],
    });

    render(
      <MemoryRouter initialEntries={["/runs/run-1"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("로그인이 필요합니다")).toBeTruthy();
    expect(screen.queryByText("Mock V2 Topic Workspace")).toBeNull();
    expect(screen.getAllByRole("button", { name: "로그인" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("link", { name: "레거시 실행" })).toBeNull();
  });

  it("renders a stable configuration error instead of offering a broken login flow", async () => {
    apiMocks.getAuthMe.mockResolvedValue({
      auth_mode: "oidc",
      authenticated: false,
      is_admin: false,
      user: null,
      login_url: null,
      logout_url: "http://127.0.0.1:8000/auth/logout",
      mcp_url: "http://127.0.0.1:8000/mcp",
      mcp_resource_metadata_url: "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp",
      product_auth_server_side_only: true,
      provider_auth_server_side_only: true,
      product_session_backend: "server_side_file",
      session_configuration_status: "invalid",
      session_configuration_reason_code: "session_secret_missing",
      warnings: ["OIDC session configuration is invalid; authenticated sessions are unavailable."],
    });

    render(
      <MemoryRouter initialEntries={["/runs/run-1"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("OIDC 세션 구성이 올바르지 않아 운영자가 설정을 수정해야 합니다.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "로그인" })).toBeNull();
    expect(screen.queryByText("Mock V2 Topic Workspace")).toBeNull();
  });

  it("renders session status and logout flow for authenticated oidc sessions", async () => {
    const user = userEvent.setup();

    apiMocks.getAuthMe
      .mockResolvedValueOnce({
        auth_mode: "oidc",
        authenticated: true,
        is_admin: true,
        user: {
          user_id: "user-1",
          display_name: "Signed In User",
          email: "user@example.com",
          auth_source: "oidc",
        },
        login_url: "http://127.0.0.1:8000/auth/login",
        logout_url: "http://127.0.0.1:8000/auth/logout",
        mcp_url: "http://127.0.0.1:8000/mcp",
        mcp_resource_metadata_url: "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp",
        product_auth_server_side_only: true,
        provider_auth_server_side_only: true,
        warnings: [],
      })
      .mockResolvedValueOnce({
        auth_mode: "oidc",
        authenticated: false,
        is_admin: false,
        user: null,
        login_url: "http://127.0.0.1:8000/auth/login",
        logout_url: "http://127.0.0.1:8000/auth/logout",
        mcp_url: "http://127.0.0.1:8000/mcp",
        mcp_resource_metadata_url: "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp",
        product_auth_server_side_only: true,
        provider_auth_server_side_only: true,
        warnings: [],
      });
    apiMocks.logoutAuth.mockResolvedValue({ auth_mode: "oidc", logged_out: true });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Signed In User")).toBeTruthy();
    expect(await screen.findByText("Mock V2 Redirect: dashboard")).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "연구 주제 탐색" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "로그아웃" }));

    await waitFor(() => {
      expect(apiMocks.logoutAuth).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText("로그인이 필요합니다")).toBeTruthy();
    expect(screen.getByText("로그아웃되었습니다.")).toBeTruthy();
  });
});
