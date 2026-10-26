import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import type { ExecutionDetail, ExecutionEvent } from "../../lib/journalExecutions";
import { JournalExecutionEvidence } from "../views/JournalExecutionEvidence";

const request = { instructions: "Keep the exact evidence.\nDo not guess.", prompt: '<img src="https://private.invalid/pixel"> & e\u0301',
  schema_name: "JOURNAL_PROPOSAL_V1", schema: { type: "object", properties: { title: { type: "string" } } }, metadata: { packet_id: "packet" }, temperature: 0.2, max_output_tokens: 4000 };
const event: ExecutionEvent = { sequence: 1, state: "LEASED", attempt: 1, at: null, deadline: null, outcome_unknown: false, reason: null };
const saved: ExecutionDetail = { contract: { execution_id: "exec", actor_id: "owner", created_at: "2026-09-28T00:00:00Z", packet: { packet_id: "packet" },
  specification: { provider_name: "openai", model: "requested-model", max_attempts: 3, max_output_tokens: 4000, expected_input_digest: "frozen" }, provider_request: request }, current: event, events: [event] };

async function open(value = saved) {
  const view = render(<JournalExecutionEvidence saved={value}/>);
  fireEvent.click(screen.getByText("요청 내용·토큰 사용 기록"));
  await screen.findByRole("heading", { name: "시도별 토큰 사용 기록" });
  return view;
}
function success(usage: Record<string, unknown>): ExecutionDetail {
  const current: ExecutionEvent = { ...event, sequence: 3, state: "SUCCEEDED", result: { provider_reported: { provider_name: "openai", model: "reported-model", response_id: "response", usage } } };
  return { ...saved, current, events: [event, current] };
}

it("opens the preserved request lazily as exact inert text, including its schema and metadata", async () => {
  await open();
  expect(screen.queryByText(request.prompt)).toBeNull();
  fireEvent.click(screen.getByText("보존된 어댑터 입력 보기"));
  const prompt = await screen.findByLabelText("보존된 문헌 입력");
  expect(prompt.textContent).toBe(request.prompt);
  expect(screen.getByLabelText("보존된 지시문").textContent).toBe(request.instructions);
  expect(JSON.parse(screen.getByLabelText("보존된 출력 JSON 규격").textContent!)).toEqual(request.schema);
  expect(JSON.parse(screen.getByLabelText("보존된 요청 메타데이터").textContent!)).toEqual(request.metadata);
  expect(screen.queryByRole("img")).toBeNull(); expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByText(/화면을 여는 동작은 외부로 전송하지 않습니다/)).toBeTruthy();
});

it("distinguishes a reported zero from missing usage and never invents a total", async () => {
  await open(success({ input_tokens: 12, output_tokens: 0 }));
  const row = screen.getByRole("region", { name: "시도 1 토큰 기록" });
  expect(within(row).getByText("보고된 입력 토큰").nextElementSibling?.textContent).toBe("12");
  expect(within(row).getByText("보고된 출력 토큰").nextElementSibling?.textContent).toBe("0");
  expect(within(row).getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("미보고");
  expect(within(row).getByText(/reported-model/)).toBeTruthy();
  expect(screen.getByText(/청구 내역이나 금액 예산이 아닙니다/)).toBeTruthy();
});

it("keeps failed and pending attempts separate from a later successful response without summing unknown usage", async () => {
  const result = success({ input_tokens: 5, output_tokens: 4, total_tokens: 9 });
  result.current = { ...result.current, attempt: 2, sequence: 6 };
  result.events = [event, { ...event, sequence: 3, state: "INTERRUPTED", outcome_unknown: true }, { ...event, sequence: 4, attempt: 2 }, result.current];
  await open(result);
  const earlier = screen.getByRole("region", { name: "시도 1 토큰 기록" });
  expect(within(earlier).getByText(/0 사용이나 무료라는 뜻은 아닙니다/)).toBeTruthy();
  expect(within(earlier).getAllByText("미보고")).toHaveLength(3);
  expect(within(screen.getByRole("region", { name: "시도 2 토큰 기록" })).getByText("9")).toBeTruthy();
  expect(screen.getByText(/미보고된 시도를 0으로 합산하지 않습니다/)).toBeTruthy();
});

it("preserves inconsistent reported numbers with a warning and does not present reserved capacity as usage", async () => {
  const result = success({ input_tokens: 12, output_tokens: 5000, total_tokens: 3 });
  result.events[0] = { ...event, dispatch: { schema_version: "JOURNAL_DISPATCH_RECEIPT_V1", actor_id: "owner", reserved_output_tokens: 4000,
    grant: { authorization_id: "grant", expires_at: "2026-09-28T00:05:00Z", max_attempts: 3, max_reserved_output_tokens: 12000 } } };
  await open(result);
  expect(screen.getByText("이 시도의 출력 토큰 예약 상한").nextElementSibling?.textContent).toBe("4000");
  expect(screen.getByText("보고된 출력 토큰").nextElementSibling?.textContent).toBe("5000");
  expect(screen.getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("3");
  expect(screen.getByText(/입력·출력의 합과 보고된 합계가 다릅니다/)).toBeTruthy();
  expect(screen.getByText(/보고된 출력 토큰이 요청 상한보다 큽니다/)).toBeTruthy();
});

it("does not coerce malformed counts, or guess fields of an unavailable request", async () => {
  await open({ ...success({ input_tokens: true, output_tokens: -1, total_tokens: "12" }), contract: { ...saved.contract, provider_request: undefined } });
  expect(screen.getAllByText("미보고")).toHaveLength(3);
  expect(screen.getByText(/보존된 어댑터 입력을 확인할 수 없습니다/)).toBeTruthy();
  expect(screen.queryByText("보존된 어댑터 입력 보기")).toBeNull();
});

it("shows no attempt for READY or cancellation at attempt zero", async () => {
  await open({ ...saved, current: { ...event, attempt: 0, state: "CANCELLED" }, events: [{ ...event, attempt: 0, state: "CANCELLED" }] });
  expect(screen.getByText("저장된 호출 시도가 없습니다.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: /토큰 기록/ })).toBeNull();
});

it("shows an unreported actual model without substituting the requested model", async () => {
  const value = success({ input_tokens: 10, output_tokens: 5 });
  const current: ExecutionEvent = { ...value.current, result: { provider_reported: { provider_name: "codex_cli", model: "not_reported", response_id: "recorded-thread", usage: { input_tokens: 10, output_tokens: 5 } } } };
  await open({ ...value, contract: { ...value.contract, specification: { ...value.contract.specification, provider_name: "codex_cli", model: "requested-codex-model" } }, current, events: [event, current] });
  const region = screen.getByRole("region", { name: "시도 1 토큰 기록" });
  expect(within(region).getByText(/보고 도구·모델: codex_cli · 실제 보고 모델 미확인 · 응답 식별자: recorded-thread/)).toBeTruthy();
  expect(within(region).queryByText(/not_reported|requested-codex-model/)).toBeNull();
  expect(within(region).getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("미보고");
});
