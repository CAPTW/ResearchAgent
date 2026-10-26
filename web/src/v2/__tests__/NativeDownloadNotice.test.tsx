import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { NativeDownloadNotice } from "../components/NativeDownloadNotice";

function emit(detail: unknown) {
  act(() => { window.dispatchEvent(new CustomEvent("xrml:download-status", { detail })); });
}
function native() {
  Object.assign(window, { __XRML_NATIVE_DOWNLOADS__: 1 });
  return render(<NativeDownloadNotice />);
}
afterEach(() => { Reflect.deleteProperty(window, "__XRML_NATIVE_DOWNLOADS__"); });

it("does not claim a save before completion and keeps its exact Unicode destination", () => {
  native();
  emit({ state: "choosing", filename: "검토.json", path: null });
  expect(screen.getByRole("status").textContent).toContain("저장 위치를 선택");
  emit({ state: "saving", filename: "검토.json", path: "D:\\결과\\검토.json" });
  expect(screen.getByRole("status").textContent).toContain("저장 중");
  expect(screen.queryByText("파일을 저장했습니다.")).toBeNull();
  emit({ state: "saved", filename: "검토.json", path: "D:\\결과\\검토.json" });
  expect(screen.getByRole("status").textContent).toContain("파일을 저장했습니다.");
  expect(screen.getByText("D:\\결과\\검토.json")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "파일 저장 안내 닫기" }));
  expect(screen.queryByRole("status")).toBeNull();
});

it("distinguishes cancellation and failure, and permits a later successful retry", () => {
  native();
  emit({ state: "cancelled", filename: "report.md", path: null });
  expect(screen.getByRole("status").textContent).toContain("저장을 취소했습니다");
  emit({ state: "failed", filename: "report.md", path: "D:\\출력\\report.md" });
  expect(screen.getByRole("alert").textContent).toContain("저장을 완료하지 못했습니다");
  expect(screen.getByRole("alert").textContent).toContain("다시 내보내세요");
  emit({ state: "saved", filename: "report.md", path: "D:\\출력\\다시.md" });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("D:\\출력\\다시.md");
});

it("ignores malformed completion data and never interprets a path as markup", () => {
  native();
  for (const detail of [null, {}, { state: "saved", filename: "x.md" }, { state: "bogus", filename: "x", path: null }]) emit(detail);
  expect(screen.queryByRole("status")).toBeNull();
  emit({ state: "saved", filename: "x.md", path: "D:\\<script>alert(1)</script>.md" });
  expect(screen.getByText("D:\\<script>alert(1)</script>.md")).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
});

it("leaves ordinary browser downloads alone and removes listeners on unmount", () => {
  const browser = render(<NativeDownloadNotice />);
  emit({ state: "saved", filename: "x", path: "D:\\x" });
  expect(screen.queryByRole("status")).toBeNull();
  browser.unmount();
  const view = native();
  view.unmount();
  emit({ state: "saved", filename: "x", path: "D:\\x" });
  expect(screen.queryByRole("status")).toBeNull();
});
