import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const html = readFileSync(resolve(process.cwd(), "../desktop/loading/index.html"), "utf8");

function loadSurface(reason?: string) {
  const query = reason ? `?${new URLSearchParams({ failure: reason })}` : "";
  window.history.replaceState(null, "", `/index.html${query}`);
  const parsed = new DOMParser().parseFromString(html, "text/html");
  document.documentElement.innerHTML = parsed.documentElement.innerHTML;
  const invoke = vi.fn().mockResolvedValue(undefined);
  Object.assign(window, { __TAURI__: { core: { invoke } } });
  const script = document.querySelector("script")!.textContent!;
  new Function("window", "document", script)(window, document);
  return {
    invoke,
    state: document.getElementById("host-state")!,
    message: document.getElementById("host-message")!,
    retry: document.getElementById("retry") as HTMLButtonElement,
  };
}

afterEach(() => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  window.history.replaceState(null, "", "/");
  Reflect.deleteProperty(window, "__TAURI__");
  Reflect.deleteProperty(window, "xrmlHostFailure");
});

describe("native failure and retry surface", () => {
  it("shows a failed connection immediately on navigation, with literal reason text", () => {
    const reason = '연결 종료 & <img src=x onerror="throw 1">';
    const { state, message, retry } = loadSurface(reason);
    expect(state.dataset.state).toBe("error");
    expect(message.textContent).toBe(reason);
    expect(message.querySelector("img")).toBeNull();
    expect(getComputedStyle(retry).display).toBe("inline-flex");
  });

  it("keeps initial startup in the loading state", () => {
    const { message, retry } = loadSurface();
    expect(message.textContent).toContain("준비하고 있습니다");
    expect(getComputedStyle(retry).display).toBe("none");
  });

  it("requests one native restart and hides retry while waiting", async () => {
    const { state, retry, invoke } = loadSurface("연결 종료");
    retry.click();
    await waitFor(() => expect(invoke).toHaveBeenCalledExactlyOnceWith("restart_sidecar"));
    expect(state.dataset.state).toBe("loading");
    expect(getComputedStyle(retry).display).toBe("none");
  });

  it("returns to an actionable error if native restart is rejected", async () => {
    const { state, message, retry, invoke } = loadSurface("연결 종료");
    invoke.mockRejectedValueOnce(new Error("unavailable"));
    retry.click();
    await waitFor(() => expect(state.dataset.state).toBe("error"));
    expect(message.textContent).toContain("다시 시작하지 못했습니다");
    expect(getComputedStyle(retry).display).toBe("inline-flex");
  });
});
