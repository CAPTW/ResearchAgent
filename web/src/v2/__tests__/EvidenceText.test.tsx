import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { EvidenceText, findEvidenceMatches } from "../components/EvidenceText";

it("preserves exact Unicode and reports code point positions, including overlapping matches", () => {
  expect(findEvidenceMatches("🙂e\u0301 한 e\u0301", "e\u0301")).toEqual([{ start: 1, end: 3 }, { start: 6, end: 8 }]);
  expect(findEvidenceMatches("🙂e\u0301", "é")).toEqual([]);
  expect(findEvidenceMatches("Banana", "ana")).toEqual([{ start: 1, end: 4 }, { start: 3, end: 6 }]);
  expect(findEvidenceMatches("A.a", ".")).toEqual([{ start: 1, end: 2 }]);
  expect(findEvidenceMatches("ABC", "abc")).toEqual([]);
  expect(findEvidenceMatches("🙂", "\ude42")).toEqual([]);
  expect(findEvidenceMatches("abc", "")).toEqual([]);
});

it("navigates matches with keyboard and keeps the original source available", () => {
  const text = "🙂e\u0301 한 e\u0301";
  render(<EvidenceText text={text}/>);
  expect(screen.getByText(text, { exact: true }).textContent).toBe(text);
  const input = screen.getByRole("textbox", { name: "근거 안에서 문구 찾기" });
  input.focus(); fireEvent.change(input, { target: { value: "e\u0301" } });
  expect(screen.getByText("일치 1 / 2 · 근거 내부 문자 위치 1–3")).toBeTruthy();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(screen.getByText("일치 2 / 2 · 근거 내부 문자 위치 6–8")).toBeTruthy();
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(screen.getByText("일치 1 / 2 · 근거 내부 문자 위치 1–3")).toBeTruthy();
  expect(document.activeElement).toBe(input);
  expect(screen.getByText("보존된 근거 전체 보기").closest("details")).toHaveProperty("open", false);
  expect(screen.getByLabelText("보존된 근거 전체").textContent).toBe(text);
  fireEvent.click(screen.getByRole("button", { name: "문구 찾기 지우기" }));
  expect(input).toHaveProperty("value", "");
  expect(document.activeElement).toBe(input);
});

it("resets the active match on a new query or source and clearly reports no literal match", () => {
  const { rerender } = render(<EvidenceText text="abc abc"/>);
  const input = screen.getByRole("textbox", { name: "근거 안에서 문구 찾기" });
  fireEvent.change(input, { target: { value: "abc" } });
  fireEvent.click(screen.getByRole("button", { name: "다음 일치" }));
  fireEvent.change(input, { target: { value: "xyz" } });
  expect(screen.getByText(/일치하는 문구가 없습니다/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "다음 일치" })).toHaveProperty("disabled", true);
  rerender(<EvidenceText text="xyz xyz"/>);
  expect(screen.getByRole("textbox")).toHaveProperty("value", "");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "xyz" } });
  expect(screen.getByText("일치 1 / 2 · 근거 내부 문자 위치 0–3")).toBeTruthy();
});

it("shows bounded surrounding context without trimming, normalizing or injecting source markup", () => {
  const text = "앞".repeat(500) + "<img src=x>" + "뒤".repeat(500);
  render(<EvidenceText text={text}/>);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "<img src=x>" } });
  const context = screen.getByLabelText("일치 문구 주변 문맥");
  expect(context.textContent).toContain("앞".repeat(160) + "<img src=x>" + "뒤".repeat(160));
  expect(context.textContent!.length).toBeLessThan(350);
  expect(context.querySelector("mark")?.textContent).toBe("<img src=x>");
  expect(context.querySelector("img")).toBeNull();
  expect(screen.getByLabelText("보존된 근거 전체").textContent).toBe(text);
});
