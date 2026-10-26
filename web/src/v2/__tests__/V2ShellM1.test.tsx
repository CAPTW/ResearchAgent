import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthSessionResponse } from "../../lib/api";

const apiMocks = vi.hoisted(() => ({
  createTopicProfile: vi.fn(),
  getApiTopics: vi.fn(),
  getTopicProfiles: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  createTopicProfile: apiMocks.createTopicProfile,
  getApiTopics: apiMocks.getApiTopics,
  getTopicProfiles: apiMocks.getTopicProfiles,
}));

import { V2Shell } from "../V2Shell";

const auth = {
  auth_mode: "disabled" as const,
  authenticated: true,
  is_admin: true,
  user: null,
  login_url: null,
  logout_url: "/auth/logout",
  mcp_url: null,
  mcp_resource_metadata_url: null,
  product_auth_server_side_only: true,
  provider_auth_server_side_only: true,
  product_session_backend: "local_internal",
  warnings: [],
} satisfies AuthSessionResponse;

beforeEach(() => {
  apiMocks.createTopicProfile.mockReset();
  apiMocks.getApiTopics.mockReset();
  apiMocks.getTopicProfiles.mockReset();
  apiMocks.getApiTopics.mockResolvedValue({
    items: Array.from({ length: 8 }, (_, index) => ({
      id: `topic-${index + 1}`,
      name: `Topic ${index + 1}`,
      description: `Research topic ${index + 1}`,
      updatedAt: "2026-08-26T00:00:00Z",
      status: "active",
      isFavorite: index === 0,
    })),
  });
  apiMocks.getTopicProfiles.mockResolvedValue({ items: [] });
});

describe("M1 topic shell interaction", () => {
  it("operates keyboard search, expandable topics, compatibility help, and dialog focus restore", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/topics/topic-1/dashboard"]}>
        <V2Shell auth={auth} onLogout={vi.fn()}>
          <div>workspace</div>
        </V2Shell>
      </MemoryRouter>,
    );

    const search = screen.getByRole("searchbox", { name: "주제, 키워드, 논문 검색" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Topic 7/ })).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Topic 8/ })).toBeNull();

    await user.keyboard("{Control>}k{/Control}");
    expect(document.activeElement).toBe(search);

    await user.click(screen.getByRole("button", { name: /더보기/ }));
    expect(screen.getByRole("button", { name: /Topic 8/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /접기/ }).getAttribute("aria-expanded")).toBe("true");

    const help = screen.getByRole("link", { name: "사용 가이드" });
    expect(help.getAttribute("href")).toContain("view=guide");
    expect(screen.getByText("로컬 단일 사용자")).toBeTruthy();

    const addTopic = screen.getByRole("button", { name: /새 주제 추가/ });
    await user.click(addTopic);
    const dialog = screen.getByRole("dialog", { name: "새 연구 주제 추가" });
    expect(dialog).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("주제 이름"));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "새 연구 주제 추가" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(addTopic));
  });
});
