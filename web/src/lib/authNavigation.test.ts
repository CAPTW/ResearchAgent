import { describe, expect, it } from "vitest";

import {
  buildLoginNavigationUrl,
  currentAppReturnTarget,
  sanitizeFrontendReturnTarget,
} from "./authNavigation";

describe("auth navigation return-target policy", () => {
  it.each([
    "/app",
    "/app/",
    "/app/runs/abc123",
    "/app/reports?tab=versions",
    "/app/provenance#detail",
  ])("preserves safe app-local target %s", (target) => {
    expect(sanitizeFrontendReturnTarget(target, "/app/")).toBe(target);
  });

  it.each([
    "https://evil.example/path",
    "http://evil.example",
    "//evil.example/path",
    "///evil.example",
    "\\evil.example",
    "/\\evil.example",
    "/app/%2f%2fevil.example",
    "/app/%5cevil.example",
    "/app/%252f%252fevil.example",
    "/app/%255cevil.example",
    "/app/%",
    "/app/%zz",
    "javascript:alert(1)",
    "data:text/html,unsafe",
    "/app?next=https://evil.example/path",
    "/app?return_to=//evil.example",
    "/app\r\nLocation:https://evil.example",
    "/app\u0000hidden",
    "/app/../outside",
  ])("fails closed for unsafe target %s", (target) => {
    expect(sanitizeFrontendReturnTarget(target, "/app/")).toBe("/app");
  });

  it("builds login navigation from path, query, and fragment without using an absolute page URL", () => {
    const returnTarget = currentAppReturnTarget(
      {
        pathname: "/app/runs/demo",
        search: "?tab=summary",
        hash: "#detail",
      },
      "/app/",
    );
    const loginUrl = buildLoginNavigationUrl(
      "http://127.0.0.1:8000/auth/login",
      returnTarget,
      "/app/",
    );
    const parsed = new URL(loginUrl);

    expect(returnTarget).toBe("/app/runs/demo?tab=summary#detail");
    expect(parsed.searchParams.get("return_to")).toBe("/app/runs/demo?tab=summary#detail");
    expect(parsed.searchParams.get("return_to")).not.toContain("http://");
  });

  it("replaces unsafe navigation before adding it to the login request", () => {
    const loginUrl = buildLoginNavigationUrl(
      "http://127.0.0.1:8000/auth/login?source=app",
      "https://evil.example/escape",
      "/app/",
    );
    const parsed = new URL(loginUrl);

    expect(parsed.searchParams.get("source")).toBe("app");
    expect(parsed.searchParams.get("return_to")).toBe("/app");
    expect(loginUrl).not.toContain("evil.example");
  });

  it("uses the configured base path and bounds target length", () => {
    expect(sanitizeFrontendReturnTarget("/research/runs", "/research/")).toBe("/research/runs");
    expect(sanitizeFrontendReturnTarget(`/research/${"a".repeat(2048)}`, "/research/")).toBe("/research");
  });
});
