import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { saveExport } from "../../lib/saveExport";

beforeEach(() => { Object.assign(URL, { createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() }); });
const notices: unknown[] = [];
const listener = (event: Event) => notices.push((event as CustomEvent).detail);
function native(invoke: ReturnType<typeof vi.fn>) {
  Object.assign(window, { __XRML_NATIVE_DOWNLOADS__: 1, __TAURI__: { core: { invoke } } });
  window.addEventListener("xrml:download-status", listener);
}
afterEach(() => {
  Reflect.deleteProperty(window, "__XRML_NATIVE_DOWNLOADS__"); Reflect.deleteProperty(window, "__TAURI__");
  window.removeEventListener("xrml:download-status", listener); notices.length = 0;
  vi.restoreAllMocks(); vi.useRealTimers();
});

it("sends exact text without a destination and waits for durable native success", async () => {
  let complete!: (result: unknown) => void;
  const invoke = vi.fn(() => new Promise(resolve => { complete = resolve; }));
  native(invoke);
  const content = "원문\r\n😀\n", path = "D:\\내보내기\\한글.md";
  const pending = saveExport(content, "원문.md", "text/markdown");
  expect(invoke).toHaveBeenCalledExactlyOnceWith("save_text_export", { content, filename: "원문.md" });
  expect(notices).toEqual([{ state: "choosing", filename: "원문.md", path: null }]);
  complete({ state: "saved", path, bytes: new TextEncoder().encode(content).length });
  await pending;
  expect(notices.at(-1)).toEqual({ state: "saved", filename: "원문.md", path });
});

it("distinguishes cancellation without claiming success or starting a browser download", async () => {
  const create = vi.spyOn(URL, "createObjectURL");
  native(vi.fn().mockResolvedValue({ state: "cancelled", path: null, bytes: 0 }));
  await saveExport("text", "x.md", "text/markdown");
  expect(notices.at(-1)).toEqual({ state: "cancelled", filename: "x.md", path: null });
  expect(create).not.toHaveBeenCalled();
});

it.each([
  { state: "saved", path: null, bytes: 4 },
  { state: "saved", path: "D:\\x.md", bytes: 2 },
  { state: "cancelled", path: "D:\\x.md", bytes: 0 },
  null,
])("rejects malformed native results without download fallback: %j", async result => {
  const create = vi.spyOn(URL, "createObjectURL");
  native(vi.fn().mockResolvedValue(result));
  await expect(saveExport("text", "x.md", "text/markdown")).rejects.toThrow("저장을 완료하지 못했습니다");
  expect(notices.at(-1)).toMatchObject({ state: "failed", filename: "x.md", path: null, reason: "저장 결과를 확인할 수 없습니다. 대상 파일을 먼저 확인하세요." });
  expect(create).not.toHaveBeenCalled();
});

it("handles native failure, busy and later user retry without automatic replay", async () => {
  const invoke = vi.fn().mockRejectedValueOnce("export_write_failed").mockRejectedValueOnce("export_busy")
    .mockResolvedValueOnce({ state: "saved", path: "D:\\x.md", bytes: 4 });
  native(invoke);
  await expect(saveExport("text", "x.md", "text/markdown")).rejects.toThrow();
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(notices.at(-1)).toMatchObject({ reason: "선택한 위치에 쓸 수 없습니다. 다른 위치 또는 파일명을 선택하세요." });
  await expect(saveExport("text", "x.md", "text/markdown")).rejects.toThrow("진행 중");
  expect(notices.at(-1)).toMatchObject({ state: "busy" });
  await saveExport("text", "x.md", "text/markdown");
  expect(invoke).toHaveBeenCalledTimes(3);
  expect(notices.at(-1)).toMatchObject({ state: "saved" });
});

it("explains an unavailable native bridge without silently downloading elsewhere", async () => {
  Object.assign(window, { __XRML_NATIVE_DOWNLOADS__: 1 });
  window.addEventListener("xrml:download-status", listener);
  await expect(saveExport("text", "x.md", "text/markdown")).rejects.toThrow("저장 연결이 없습니다");
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

it("keeps the ordinary browser download and delayed object URL cleanup", async () => {
  vi.useFakeTimers();
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await saveExport("original\r\n", "source.json", "application/json");
  expect(create).toHaveBeenCalledOnce(); expect(click).toHaveBeenCalledOnce();
  expect(revoke).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1000);
  expect(revoke).toHaveBeenCalledWith("blob:test");
});
