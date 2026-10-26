import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import { AppearanceProvider } from "../AppearanceProvider";
import { APPEARANCE_STORAGE_KEY, DEFAULT_APPEARANCE, formatTimestamp } from "../appearance";
import { TemporalMemoryPanel } from "../views/TemporalMemoryPanel";
import * as api from "../../lib/temporalMemory";

vi.mock("../../lib/temporalMemory", async (original) => ({
  ...await original<typeof api>(), listSnapshots: vi.fn(), captureSnapshot: vi.fn(), getDiff: vi.fn(),
}));
const snap = { snapshot_id: "tsnap_one", topic_id: "topic", created_at: "2026-09-18T01:00:00Z", parent_snapshot_id: null, corpus_digest: "digest", paper_count: 1 };
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.listSnapshots).mockResolvedValue({ items: [], total: 0 } as never); });
function mount() { render(<MemoryRouter><TemporalMemoryPanel topicId="topic" /></MemoryRouter>); }
it("zero then one snapshot, keyboard capture, duplicate message and provenance", async () => {
  mount();
  expect(await screen.findByText(/아직.*스냅샷/)).toBeTruthy();
  vi.mocked(api.listSnapshots).mockResolvedValue({ items: [snap], total: 1 } as never);
  vi.mocked(api.captureSnapshot).mockResolvedValue({ snapshot: snap, created: true } as never);
  const button = screen.getByRole("button", { name: "현재 상태 스냅샷 만들기" });
  button.focus(); expect(document.activeElement).toBe(button); fireEvent.click(button);
  expect(await screen.findByText(/스냅샷이 하나/)).toBeTruthy();
  expect(screen.getByText("digest")).toBeTruthy();
  vi.mocked(api.captureSnapshot).mockResolvedValue({ snapshot: snap, created: false } as never);
  fireEvent.click(button);
  expect(await screen.findByText(/동일한 상태.*기존/)).toBeTruthy();
});
it("list error is independent and never becomes empty", async () => {
  vi.mocked(api.listSnapshots).mockRejectedValue(new Error("broken")); mount();
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.queryByText(/아직.*스냅샷/)).toBeNull();
});
it("capture busy and failure", async () => {
  let reject!: (e: Error) => void;
  vi.mocked(api.captureSnapshot).mockReturnValue(new Promise((_, r) => { reject = r; }));
  mount(); await screen.findByText(/아직.*스냅샷/);
  fireEvent.click(screen.getByRole("button", { name: "현재 상태 스냅샷 만들기" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "현재 상태 스냅샷 만들기" }) as HTMLButtonElement).disabled).toBe(true));
  reject(new Error("capture failed")); expect(await screen.findByRole("alert")).toBeTruthy();
});

it("pages through all snapshots and preserves an off-page duplicate capture", async () => {
  const older={...snap,snapshot_id:"tsnap_old",created_at:"2026-09-01T00:00:00Z"};
  vi.mocked(api.listSnapshots).mockImplementation(async (_topic,page=1)=>({items:page===1?[snap]:[older],total:26,page,pageSize:25}) as never);
  mount();await screen.findByLabelText("스냅샷 이력");
  fireEvent.click(screen.getByRole("button",{name:"다음 스냅샷 페이지"}));
  await waitFor(()=>expect(api.listSnapshots).toHaveBeenLastCalledWith("topic",2));
  await screen.findByText(older.snapshot_id);
  vi.mocked(api.captureSnapshot).mockResolvedValue({snapshot:older,created:false} as never);
  fireEvent.click(screen.getByRole("button",{name:"현재 상태 스냅샷 만들기"}));
  await screen.findByText(/동일한 상태.*기존/);
  await waitFor(()=>expect(api.listSnapshots).toHaveBeenLastCalledWith("topic",1));
  expect((screen.getByLabelText("스냅샷 이력") as HTMLSelectElement).value).toBe(older.snapshot_id);
});

const second = { ...snap, snapshot_id: "tsnap_two", parent_snapshot_id: snap.snapshot_id, algorithm_version: "xrml-local-snapshot-diff-v1", acceptance_state: "ACCEPTED_LOCAL_DETERMINISTIC" };
function comparison(events: unknown[] = []) {
  return { from_snapshot: snap, to_snapshot: second, events, counts: Object.fromEntries(api.eventTypes.map(t => [t, events.length ? 1 : 0])), algorithm_version: second.algorithm_version };
}
function two() { vi.mocked(api.listSnapshots).mockResolvedValue({ items: [second, snap], total: 2 } as never); }
it("valid empty pair states only local authoritative comparison", async () => {
  two(); vi.mocked(api.getDiff).mockResolvedValue(comparison() as never); mount();
  expect(await screen.findByText(/두 스냅샷 사이.*변경이 감지되지/)).toBeTruthy();
  expect(screen.getByText(second.algorithm_version)).toBeTruthy();
  expect(screen.getByText(second.snapshot_id)).toBeTruthy();
});
it("eight counts, exact ordered events, paper and evidence destinations", async () => {
  two();
  const events = api.eventTypes.map((type, index) => ({ event_id: String(index), event_type: type,
    canonical_paper_id: "paper-a", paper_version_id: "version-a", before_digest: "before", after_digest: "after", before_state: "null", after_state: "null",
    from_snapshot_id: snap.snapshot_id, to_snapshot_id: second.snapshot_id, source_snapshot_id: second.snapshot_id,
    source_entity_ids: ["paper-a"], source_evidence_ids: ["evidence-a"], acceptance_state: second.acceptance_state, algorithm_version: second.algorithm_version,
    paper_destination: "/topics/topic/papers?paper=version-a", evidence_destinations: [{id: "evidence-a", destination: "/topics/topic/papers?paper=version-a&evidence=evidence-a"}],
  }));
  vi.mocked(api.getDiff).mockResolvedValue(comparison(events) as never); mount();
  await screen.findByRole("list");
  const items = screen.getAllByRole("listitem"); expect(items).toHaveLength(8);
  events.forEach((event, i) => expect(items[i].textContent).toContain(event.event_type));
  expect(screen.getAllByRole("link", { name: /논문 버전/ })[0].getAttribute("href")).toBe("/topics/topic/papers?paper=version-a");
  expect(screen.getAllByRole("link", { name: "근거 evidence-a" })[0].getAttribute("href")).toContain("&evidence=evidence-a");
  api.eventTypes.forEach(type => expect(document.querySelector(`[data-event-count="${type}"]`)?.textContent).toBe("1"));
});
it.each([false, true])("list failure unavailable=%s never renders zero snapshots", async (unavailable) => {
  vi.mocked(api.listSnapshots).mockRejectedValue(unavailable ? new ApiError("offline", 503, "") : new Error("failed"));
  mount(); await screen.findByRole("alert"); expect(screen.queryByText(/아직.*스냅샷/)).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain(unavailable ? "사용 불가" : "오류");
});
it.each([false, true])("diff failure unavailable=%s does not conceal snapshot or show empty changes", async (unavailable) => {
  two(); vi.mocked(api.getDiff).mockRejectedValue(unavailable ? new ApiError("conflict", 409, "") : new Error("failed"));
  mount(); await screen.findByRole("alert"); expect(screen.getByText(second.snapshot_id)).toBeTruthy();
  expect(screen.queryByText(/변경이 감지되지/)).toBeNull();
});
it("independent list and diff loading", async () => {
  let resolve!: (v: unknown) => void;
  vi.mocked(api.listSnapshots).mockReturnValue(new Promise(r => { resolve = r as never; }));
  vi.mocked(api.getDiff).mockReturnValue(new Promise(() => {})); mount();
  expect(screen.getByText(/목록을 불러오는 중/)).toBeTruthy();
  resolve({items: [second, snap], total: 2});
  expect(await screen.findByText(/변경을 불러오는 중/)).toBeTruthy();
  expect(screen.getByText(second.snapshot_id)).toBeTruthy();
});
it("incomplete provenance is unavailable with no false detail destination", async () => {
  two(); vi.mocked(api.getDiff).mockResolvedValue(comparison([{event_id: "e", event_type: "PAPER_ADDED"}]) as never); mount();
  expect(await screen.findByText(/출처가 불완전/)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});
it("keeps the historical event visible when live evidence is unavailable and retries navigation", async () => {
  two();
  const event = {
    event_id: "e", event_type: "EVIDENCE_CHANGED", canonical_paper_id: "paper-a", paper_version_id: "version-a",
    before_digest: "before", after_digest: "after", before_state: "null", after_state: "captured",
    from_snapshot_id: snap.snapshot_id, to_snapshot_id: second.snapshot_id, source_snapshot_id: second.snapshot_id,
    source_entity_ids: ["paper-a"], source_evidence_ids: ["evidence-a"], acceptance_state: second.acceptance_state,
    algorithm_version: second.algorithm_version, paper_destination: null, evidence_destinations: [],
    unavailable_evidence_ids: ["evidence-a"], destination_reason: "CURRENT_DESTINATIONS_UNAVAILABLE",
  };
  vi.mocked(api.getDiff).mockResolvedValue(comparison([event]) as never);
  mount();
  expect(await screen.findByText(/현재 원문이 없거나 캡처 당시 내용과 달라/)).toBeTruthy();
  expect(screen.getByText("EVIDENCE_CHANGED")).toBeTruthy();
  expect(screen.getByText(/저장된 변경 기록은 유지됩니다/)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
  expect(document.querySelector('[data-event-count="EVIDENCE_CHANGED"]')?.textContent).toBe("1");
  vi.mocked(api.getDiff).mockResolvedValue(comparison([{...event, destination_reason: null, unavailable_evidence_ids: [],
    paper_destination: "/topics/topic/papers?paper=version-a", evidence_destinations: [{id: "evidence-a", destination: "/topics/topic/papers?paper=version-a&evidence=evidence-a"}],
  }]) as never);
  fireEvent.click(screen.getByRole("button", {name: "비교 다시 불러오기"}));
  expect(await screen.findByRole("link", {name: "근거 evidence-a"})).toBeTruthy();
  expect(screen.queryByText(/캡처 당시 내용과 달라/)).toBeNull();
  expect(api.captureSnapshot).not.toHaveBeenCalled();
});
it.each(["12h", "24h"] as const)("uses stored %s time preference", async (timeFormat) => {
  localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify({...DEFAULT_APPEARANCE, timeFormat}));
  document.cookie = "xrml_appearance_v1=; max-age=0; path=/";
  vi.mocked(api.listSnapshots).mockResolvedValue({ items: [snap], total: 1 } as never);
  render(<MemoryRouter><AppearanceProvider><TemporalMemoryPanel topicId="topic" /></AppearanceProvider></MemoryRouter>);
  expect(await screen.findByText(formatTimestamp(snap.created_at, timeFormat, "ko-KR"))).toBeTruthy();
  localStorage.removeItem(APPEARANCE_STORAGE_KEY);
  document.cookie = "xrml_appearance_v1=; max-age=0; path=/";
});
