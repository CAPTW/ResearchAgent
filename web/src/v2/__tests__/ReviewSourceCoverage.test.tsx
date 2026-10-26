import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewSourceCoverage } from "../components/ReviewSourceCoverage";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic%20one/paper-bank/version%2Fone/review-preparations";
const value = {
  schema_version: "PAPER_REVIEW_SOURCE_COVERAGE_V1", anchor_preparation_id: "prep/one",
  source: { run_id: "run-1", paper_id: "paper-1", source_digest: "a".repeat(64), source_chars: 20, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 25, artifact_ref: "run:raw/source.txt", extraction_truncation: "UNKNOWN" },
  normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT",
  prepared: { record_count: 2, covered_chars: 8, ranges: [{ start: 10, end: 18 }], gaps: [{ start: 0, end: 10 }, { start: 18, end: 20 }], records: [{ preparation_id: "prep/one", start: 10, end: 15 }, { preparation_id: "prep-two", start: 13, end: 18 }] },
  inputs: [
    { input_origin: "FIXTURE_SYNTHETIC", record_count: 1, covered_chars: 5, ranges: [{ start: 10, end: 15 }], records: [{ receipt_id: "receipt/one", preparation_id: "prep/one", start: 10, end: 15 }] },
    { input_origin: "PROVIDER_RESPONSE", record_count: 0, covered_chars: 0, ranges: [], records: [] },
    { input_origin: "MANUAL_IMPORT_UNVERIFIED", record_count: 0, covered_chars: 0, ranges: [], records: [] },
  ],
  unmapped_receipt_count: 3, other_source_receipt_count: 1,
  full_document_review: "NOT_ESTABLISHED", scientific_support: "NOT_ASSESSED",
};
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(value); });
async function open() { fireEvent.click(screen.getByText("보존된 출처의 범위 요약")); return screen.findByRole("region", { name: "보존된 출처 범위" }); }

it("loads on disclosure, uses original coordinates and separates prepared, synthetic, provider and imported records", async () => {
  const prep = vi.fn(), receipt = vi.fn();
  render(<ReviewSourceCoverage root={root} id="prep/one" onOpenPreparation={prep} onOpenReceipt={receipt}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  const region = await open();
  expect(api.fetchJson).toHaveBeenCalledWith(`${root}/prep%2Fone/coverage`);
  expect(within(region).getByText("준비 기록 2건 · 중복 제외 8자")).toBeTruthy();
  expect(within(region).getByText("준비되지 않은 저장 본문 범위: 0–10, 18–20")).toBeTruthy();
  expect(within(region).getByText(/전체 논문 검토 미확인 · 과학적 뒷받침 미평가/)).toBeTruthy();
  expect(within(region).getByText(/보존된 정규화 본문 20자/)).toBeTruthy();
  for (const label of ["합성 입력·결과", "Provider 응답 기록", "수동 반입 기록"]) expect(within(region).getByRole("heading", { name: label })).toBeTruthy();
  expect(within(region).getByText(/출처 범위에 연결되지 않은 입력 기록 3건/)).toBeTruthy();
  expect(within(region).getByText(/다른 출처에 연결된 입력 기록 1건/)).toBeTruthy();
  fireEvent.click(within(region).getByText("준비 기록별 범위"));
  const button = await within(region).findByRole("button", { name: "준비 기록 열기 · prep-two" });
  button.focus(); await userEvent.setup().keyboard("{Enter}");
  expect(prep).toHaveBeenCalledWith("prep-two");
  fireEvent.click(within(region).getByText("합성 입력·결과 기록별 범위"));
  fireEvent.click(await within(region).findByRole("button", { name: "입력·결과 열기 · receipt/one" }));
  expect(receipt).toHaveBeenCalledWith("receipt/one");
});

it("removes old totals on refresh failure and retries without a write or execution", async () => {
  render(<ReviewSourceCoverage root={root} id="prep/one"/>); await open();
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("review_source_coverage_limit_exceeded"));
  fireEvent.click(screen.getByRole("button", { name: "범위 요약 새로고침" }));
  expect((await screen.findByRole("alert")).textContent).toContain("기록 수가 조회 한도를 넘어");
  expect(screen.queryByText("준비 기록 2건 · 중복 제외 8자")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "범위 요약 새로고침" }));
  await screen.findByText("준비 기록 2건 · 중복 제외 8자");
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});

it("ignores a delayed old-topic response when the selected source changes", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(done => { resolve = done; }) as never);
  const view = render(<ReviewSourceCoverage root={root} id="prep/one"/>);
  fireEvent.click(screen.getByText("보존된 출처의 범위 요약"));
  await screen.findByText("범위 요약 확인 중");
  view.rerender(<ReviewSourceCoverage root="/other-topic" id="prep-two"/>);
  expect(screen.queryByText("범위 요약 확인 중")).toBeNull();
  vi.mocked(api.fetchJson).mockResolvedValue({ ...value, anchor_preparation_id: "prep-two", prepared: { ...value.prepared, record_count: 1, covered_chars: 5 } });
  await open(); await screen.findByText("준비 기록 1건 · 중복 제외 5자");
  await act(async () => resolve(value));
  expect(screen.queryByText("준비 기록 2건 · 중복 제외 8자")).toBeNull();
});

it("keeps empty input categories honest and shows integrity failure as unavailable", async () => {
  render(<ReviewSourceCoverage root={root} id="prep/one"/>); await open();
  const provider = screen.getByRole("region", { name: "Provider 응답 기록" });
  expect(within(provider).getByText("입력·결과 기록 0건 · 중복 제외 0자")).toBeTruthy();
  expect(within(provider).getByText("기록된 입력 범위: 없음")).toBeTruthy();
  expect(within(provider).getByText(/실제 모델 실행이나 과학적 검토 완료를 별도로 입증하지 않습니다/)).toBeTruthy();
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("review_preparation_integrity_unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "범위 요약 새로고침" }));
  expect((await screen.findByRole("alert")).textContent).toContain("범위 요약 조회 오류");
  expect(screen.queryByRole("region", { name: "Provider 응답 기록" })).toBeNull();
});

it("adds validated saved result gaps separately from prepared and origin-only input counts", async () => {
  const empty = { record_count: 0, covered_chars: 0, ranges: [], records: [] };
  const returned = { start: 10, end: 15, preparation_id: "prep/one", receipt_id: "provider-one", source: value.source, selection: { start: 10, end: 15, chars: 5, sha256: "c".repeat(64) }, class: "BODY_GROUNDED", subtype: "NATIVE_ANTHROPIC_V2" };
  const group = { record_count: 1, covered_chars: 5, ranges: [{ start: 10, end: 15 }], records: [returned], gaps: [{ start: 0, end: 10 }, { start: 15, end: 20 }] };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...value, results: { validated_returned: group, body_grounded: group, other_returned: empty, unverified_provider: empty, gap_semantics: "NO_VALIDATED_SAVED_RESULT", attempt_history_included: false } });
  render(<ReviewSourceCoverage root={root} id="prep/one"/>); await open();
  expect(screen.getByText("준비되지 않은 저장 본문 범위: 0–10, 18–20")).toBeTruthy();
  expect(screen.getByText("저장된 본문 결과가 없는 범위: 0–10, 15–20")).toBeTruthy();
  expect(screen.getByText(/실패한 요청이나 미실행을 뜻하지 않습니다/)).toBeTruthy();
  expect(screen.getByRole("region", { name: "Provider 출처만 있는 미검증 기록" })).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});
