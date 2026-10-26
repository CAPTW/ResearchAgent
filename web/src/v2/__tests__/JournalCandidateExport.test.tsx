import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalExchangePanel, SavedCandidate } from "../views/JournalExchangePanel";

const candidate = { candidate_id: "candidate-one", imported_by: "owner", imported_at: "2026-10-15T00:00:00Z", raw_json: "{}", raw_sha256: "a".repeat(64),
  proposal: { title: "Preserved", sections: [] }, packet: { packet_id: "packet-one", references: [] }, citations: [], markdown: "# Preserved",
  coverage: { selected_papers: 0, cited_papers: 0, abstract_citations: 0, excerpt_citations: 0, uncited_paper_version_ids: [] } };
const nativeURL = URL, blobs: Blob[] = [];
async function contents(blob: Blob) { return new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsText(blob); }); }
beforeEach(() => {
  vi.restoreAllMocks(); blobs.length = 0;
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal("URL", class extends nativeURL { static createObjectURL(blob: Blob) { blobs.push(blob); return "blob:journal-export"; } static revokeObjectURL() {} });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("downloads the exact server candidate JSON preserving number spelling and original Unicode", async () => {
  const raw = JSON.stringify({ ...candidate, source: { timeout_seconds: 120, text: "e\u0301한🙂" } }).replace('"timeout_seconds":120', '"timeout_seconds":120.0');
  vi.spyOn(api, "fetchJson").mockResolvedValue(candidate); vi.mocked(fetch).mockResolvedValue(new Response(raw, { status: 200 }));
  render(<SavedCandidate root="/candidates" candidateId="candidate-one"/>); await screen.findByRole("button", { name: "원본 후보·근거 JSON 내려받기" });
  fireEvent.click(screen.getByRole("button", { name: "원본 후보·근거 JSON 내려받기" })); await waitFor(() => expect(blobs).toHaveLength(1));
  expect(await contents(blobs[0])).toBe(raw); expect(fetch).toHaveBeenCalledExactlyOnceWith(`${api.getApiBase()}/candidates/candidate-one`, expect.objectContaining({ credentials: "include", cache: "no-store" }));
});

it("downloads the exact fixed packet response while retaining an independently selected import file", async () => {
  const raw = '{"packet_id":"packet-one","review_inputs":[],"timeout_seconds":120.0,"text":"e\u0301한🙂"}';
  vi.spyOn(api, "fetchJson").mockResolvedValue({ items: [], total: 0, page: 1, limit: 10 }); vi.mocked(fetch).mockResolvedValue(new Response(raw, { status: 200 }));
  render(<JournalExchangePanel topicId="topic" jobId="job"/>); fireEvent.click(screen.getByText("저널 입력·결과 파일 교환")); await screen.findByText("보존된 저널 후보 · 0개");
  const content = new TextEncoder().encode("{}");
  fireEvent.change(screen.getByLabelText("저널 결과 JSON 파일"), { target: { files: [{ name: "pending.json", size: content.length, arrayBuffer: async () => content.buffer }] } });
  await screen.findByText("선택한 파일: pending.json"); fireEvent.click(screen.getByRole("button", { name: "저널 고정 입력 JSON 내려받기" }));
  await waitFor(() => expect(blobs).toHaveLength(1)); expect(await contents(blobs[0])).toBe(raw); expect(screen.getByText("선택한 파일: pending.json")).toBeTruthy();
});

it("keeps the shared unauthorized event and structured error when reading raw export text", async () => {
  const unauthorized = vi.fn(); window.addEventListener("xrml:auth-unauthorized", unauthorized);
  vi.mocked(fetch).mockResolvedValue(new Response('{"detail":"login_required"}', { status: 401 }));
  try { await expect(api.fetchText("/candidate")).rejects.toMatchObject({ status: 401, message: "login_required", body: '{"detail":"login_required"}' });
    expect(unauthorized).toHaveBeenCalledTimes(1); expect(fetch).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ credentials: "include", cache: "no-store" }));
  } finally { window.removeEventListener("xrml:auth-unauthorized", unauthorized); }
});

it("retains the existing parsed JSON client response and caller request options", async () => {
  vi.mocked(fetch).mockResolvedValue(new Response('{"timeout_seconds":120.0}', { status: 200 }));
  await expect(api.fetchJson("/existing", { method: "POST", body: "{}" })).resolves.toEqual({ timeout_seconds: 120 });
  expect(fetch).toHaveBeenCalledExactlyOnceWith(`${api.getApiBase()}/existing`, expect.objectContaining({ credentials: "include", method: "POST", body: "{}", headers: { "Content-Type": "application/json" } }));
});

it("does not download a nonmatching candidate or an HTTP failure", async () => {
  vi.spyOn(api, "fetchJson").mockResolvedValue(candidate);
  vi.mocked(fetch).mockResolvedValueOnce(new Response('{"candidate_id":"other"}', { status: 200 })).mockResolvedValueOnce(new Response('{"detail":"integrity_unavailable"}', { status: 409 }));
  render(<SavedCandidate root="/candidates" candidateId="candidate-one"/>); await screen.findByRole("button", { name: "원본 후보·근거 JSON 내려받기" }); fireEvent.click(screen.getByRole("button", { name: "원본 후보·근거 JSON 내려받기" }));
  await screen.findByRole("alert"); expect(blobs).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "선택 후보 다시 확인" })); await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "원본 후보·근거 JSON 내려받기" })); await screen.findByText(/integrity_unavailable/); expect(blobs).toHaveLength(0);
});
