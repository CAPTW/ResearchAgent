import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getApiTopics: vi.fn(),
  getTopicProfiles: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getApiTopics: apiMocks.getApiTopics,
  getTopicProfiles: apiMocks.getTopicProfiles,
}));

import { V2DefaultRoute } from "../V2TopicWorkspaceScreen";

beforeEach(() => {
  apiMocks.getApiTopics.mockReset();
  apiMocks.getTopicProfiles.mockReset();
});

describe("M1 default-route resource truth", () => {
  it("renders API failure instead of claiming an empty topic list", async () => {
    apiMocks.getApiTopics.mockRejectedValue(new Error("topic index failed"));
    apiMocks.getTopicProfiles.mockRejectedValue(new Error("profile index failed"));
    render(<MemoryRouter><V2DefaultRoute /></MemoryRouter>);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByText("등록된 연구 주제가 없습니다.")).toBeNull();
  });

  it("distinguishes a partial index outage from a verified empty response", async () => {
    apiMocks.getApiTopics.mockResolvedValue({ items: [] });
    apiMocks.getTopicProfiles.mockRejectedValue(new Error("profile index failed"));
    render(<MemoryRouter><V2DefaultRoute /></MemoryRouter>);

    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
    expect(screen.getByText("기본 주제를 결정할 수 없습니다.")).toBeTruthy();
    expect(screen.queryByText("등록된 연구 주제가 없습니다.")).toBeNull();
  });
});
