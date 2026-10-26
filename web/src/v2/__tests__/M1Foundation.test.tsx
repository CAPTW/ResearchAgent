import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  APPEARANCE_COOKIE_KEY,
  APPEARANCE_STORAGE_KEY,
  DEFAULT_APPEARANCE,
  applyAppearance,
  loadAppearanceCookie,
  loadAppearance,
  resolveMotion,
  resolveTheme,
  saveAppearance,
  serializeAppearanceCookie,
} from "../appearance";
import { AppearanceProvider } from "../AppearanceProvider";
import { V2CompatibilityRoute, describeRouteIntent, V2_TOPIC_TABS } from "../routeIntent";
import {
  failResource,
  initialResourceState,
  resolveResource,
  retryResource,
  startResource,
} from "../resourceState";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.cookie = `${APPEARANCE_COOKIE_KEY}=; Path=/; Max-Age=0; SameSite=Lax`;
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-accent");
  document.documentElement.removeAttribute("data-density");
  document.documentElement.removeAttribute("data-motion");
});

describe("M1 appearance contract", () => {
  it("resolves System live while explicit themes ignore OS changes", () => {
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("solarized-light", true)).toBe("solarized-light");
    expect(resolveTheme("solarized-dark", false)).toBe("solarized-dark");
    expect(resolveMotion("system", true)).toBe("reduced");
    expect(resolveMotion("full", true)).toBe("full");
  });

  it("persists only typed preferences and applies semantic document state", () => {
    const preferences = {
      theme: "solarized-light" as const,
      accent: "violet" as const,
      density: "compact" as const,
      motion: "reduced" as const,
    };
    saveAppearance(window.localStorage, preferences);
    expect(loadAppearance(window.localStorage)).toEqual(preferences);

    window.localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify({ ...preferences, theme: "neon" }));
    expect(loadAppearance(window.localStorage)).toEqual(DEFAULT_APPEARANCE);

    applyAppearance(document.documentElement, preferences, { prefersDark: true, prefersReducedMotion: false });
    expect(document.documentElement.dataset.theme).toBe("solarized-light");
    expect(document.documentElement.dataset.accent).toBe("violet");
    expect(document.documentElement.dataset.density).toBe("compact");
    expect(document.documentElement.dataset.motion).toBe("reduced");
  });

  it("serializes a top-level-navigation cookie so native preferences survive ephemeral sidecar ports", () => {
    const preferences = {
      theme: "solarized-dark" as const,
      accent: "violet" as const,
      density: "compact" as const,
      motion: "reduced" as const,
    };

    const cookie = serializeAppearanceCookie(preferences);

    expect(cookie).toContain(`${APPEARANCE_COOKIE_KEY}=`);
    expect(cookie).toContain("Path=/");
    expect(cookie).toContain("SameSite=Lax");
    expect(loadAppearanceCookie(`unrelated=value; ${cookie}`)).toEqual(preferences);
    expect(loadAppearanceCookie(`${APPEARANCE_COOKIE_KEY}=invalid-json`)).toBeNull();
  });
});

describe("M1 compatibility route contract", () => {
  it("keeps the approved five-tab identity", () => {
    expect(V2_TOPIC_TABS.map((tab) => tab.id)).toEqual(["dashboard", "papers", "insights", "reports", "settings"]);
  });

  it("preserves route identifiers and relevant intent parameters", () => {
    const intent = describeRouteIntent(
      "/topics/topic%20alpha/runs/run%2042",
      "?view=lineage&action=rerun&section=proof&collection=saved&provider=codex&query=agents",
    );
    expect(intent.identifiers).toEqual({ topicId: "topic alpha", runId: "run 42" });
    expect(intent.parameters).toEqual({
      view: "lineage",
      action: "rerun",
      section: "proof",
      collection: "saved",
      provider: "codex",
      query: "agents",
    });
    expect(intent.targetMilestone).toBe("M2");
  });

  it("renders an explicit accessible state instead of redirecting an unsupported report detail", () => {
    render(
      <MemoryRouter initialEntries={["/reports/report%209?view=detail&provider=codex"]}>
        <Routes>
          <Route path="/reports/:reportId" element={<V2CompatibilityRoute />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("status", { name: "호환 경로 상태" })).toBeTruthy();
    expect(screen.getByText("report 9")).toBeTruthy();
    expect(screen.getByText(/provider: codex/)).toBeTruthy();
    expect(screen.getByText(/M4/)).toBeTruthy();
  });
});

describe("M1 independent resource states", () => {
  it("keeps base failures independent from section success and targets retries", () => {
    const baseError = failResource(startResource(initialResourceState), new Error("base failed"));
    const papersReady = resolveResource(startResource(initialResourceState), { items: [] as string[] });

    expect(baseError.phase).toBe("error");
    expect(baseError.message).toBe("base failed");
    expect(papersReady.phase).toBe("empty");
    expect(baseError.phase).toBe("error");

    const retry = retryResource(baseError);
    expect(retry.phase).toBe("retrying");
    expect(retry.retryCount).toBe(1);
  });

  it("retains labelled stale data during retry", () => {
    const ready = resolveResource(startResource(initialResourceState), { value: 1 });
    const retrying = retryResource(ready);

    expect(retrying.phase).toBe("retrying");
    expect(retrying.data).toEqual({ value: 1 });
    expect(retrying.stale).toBe(true);
  });
});

describe("M1 System media behavior", () => {
  it("follows live OS changes in System mode while an explicit theme stays fixed", () => {
    const colorListeners: Array<(event: MediaQueryListEvent) => void> = [];
    const motionListeners: Array<(event: MediaQueryListEvent) => void> = [];
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        (query.includes("color-scheme") ? colorListeners : motionListeners).push(listener);
      },
      removeEventListener: vi.fn(),
    }));

    const { unmount } = render(<AppearanceProvider><span>appearance</span></AppearanceProvider>);
    expect(document.documentElement.dataset.theme).toBe("light");
    act(() => colorListeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent)));
    act(() => motionListeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent)));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.motion).toBe("reduced");
    unmount();

    const explicitPreference = { ...DEFAULT_APPEARANCE, theme: "solarized-light" as const };
    saveAppearance(window.localStorage, explicitPreference);
    document.cookie = serializeAppearanceCookie(explicitPreference);
    const explicit = render(<AppearanceProvider><span>explicit</span></AppearanceProvider>);
    act(() => colorListeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent)));
    expect(document.documentElement.dataset.theme).toBe("solarized-light");
    explicit.unmount();
  });
});
