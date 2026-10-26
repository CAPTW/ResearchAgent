import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { CodexJournalManifest, ExecutionDetail } from "../../lib/journalExecutions";
import { JournalCodexPreview } from "../views/JournalCodexPreview";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const url = "/api/topics/topic/local-synthesis/job/journal-executions/exec";
const event = { sequence: 0, state: "READY" as const, attempt: 0, at: "2026-10-06T00:00:00Z", deadline: null, outcome_unknown: false, reason: null };
const saved: ExecutionDetail = { contract: { execution_id: "exec", actor_id: "request-owner", created_at: event.at, packet: { packet_id: "packet" }, specification: { provider_name: "codex_cli", model: "fixture-model", max_attempts: 2, max_output_tokens: 4000, expected_input_digest: "frozen-input" } }, current: event, events: [] };
const manifest: CodexJournalManifest = {
  schema_version: "CODEX_JOURNAL_CLI_V1", contract_digest: "a".repeat(64), model: "fixture-model", reasoning_effort: "xhigh",
  runtime_identity: { launcher_sha256: "b".repeat(64), environment_sha256: "c".repeat(64), workspace_sha256: "d".repeat(64) },
  argv_shape: ["{explicit_launcher}", "exec", "--ignore-user-config", "--model", "fixture-model", "-c", "features.shell_tool=false"],
  stdin_sha256: "e".repeat(64), stdin_bytes: 1234, schema_sha256: "f".repeat(64), timeout_seconds: 90,
  max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_final_bytes: 512000,
  automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
};
const available = {
  schema_version: "CODEX_JOURNAL_PREVIEW_V1", preview_only: true, execution_allowed: false,
  execution_id: "exec", contract_digest: manifest.contract_digest, actor_id: "request-owner", observed_sequence: 0,
  available: true, reason: null, manifest, binding: { surface_id: "codex_cli", adapter_id: "xrml-codex-frozen-journal", adapter_version: "1", command_shape_sha256: "1".repeat(64) }, preview_digest: "2".repeat(64),
};
function unavailable(reason = "codex_preview_not_configured") { return { ...available, available: false, reason, manifest: null, binding: null, preview_digest: null }; }
beforeEach(() => vi.resetAllMocks());
function open() { return render(<JournalCodexPreview url={url} saved={saved}/>); }
function check() { fireEvent.click(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" })); }
function refresh() { fireEvent.click(screen.getByRole("button", { name: "Codex 실행 조건 다시 확인" })); }

it("fetches only on explicit click and shows current conditions separately from historical receipts", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(available);
  open();
  expect(api.fetchJson).not.toHaveBeenCalled();
  check();
  await screen.findByText(`미리보기 SHA-256: ${available.preview_digest}`);
  fireEvent.click(screen.getByText("현재 미리보기 전송 조건"));
  expect(screen.getByText("Codex CLI 실행 계획")).toBeTruthy();
  expect(screen.getByText(/현재 서버 조건으로 계산한 미리보기이며 실행 이력에 저장된 계획이 아닙니다/)).toBeTruthy();
  expect(screen.queryByText(/저장된 실행 계획이며 실제 계정/)).toBeNull();
  expect(screen.getByText(/과거 시도의 실행 기록이나 실행 승인이 아니며/)).toBeTruthy();
  expect(screen.getByText(/실행 파일 경로·바이트, 환경 변수 값, 작업 폴더 경로/)).toBeTruthy();
  expect(screen.getByText(/인증 정보 파일·설정 파일·작업 폴더 내용/)).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(`${url}/codex-preview`);
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "CLI 설정 검사 기록 확인" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /승인|실행 시작/ })).toBeNull();
  fireEvent.click(screen.getByText("요청할 명령·제한 설정"));
  expect(screen.getByLabelText("Codex 명령 형태").textContent).toBe(JSON.stringify(manifest.argv_shape, null, 2));
  expect(screen.getByText(/복사해서 실행할 실제 명령이 아니며/)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});

it.each([
  ["codex_preview_not_configured", "서버의 Codex 미리보기 실행 환경이 설정되지 않았습니다"],
  ["codex_preview_provider_unsupported", "이 요청은 Codex 미리보기를 지원하는 요청이 아닙니다"],
  ["codex_preview_configuration_invalid", "서버의 Codex 미리보기 설정을 사용할 수 없습니다"],
  ["codex_preview_configuration_changed", "서버의 Codex 미리보기 설정이 바뀌었습니다"],
  ["codex_preview_runtime_changed", "Codex 미리보기 실행 환경이 바뀌었습니다"],
  ["codex_preview_contract_unsupported", "저장된 요청 형식으로 Codex 미리보기를 만들 수 없습니다"],
])("shows a retryable safe reason without a plan: %s", async (reason, label) => {
  vi.mocked(api.fetchJson).mockResolvedValue(unavailable(reason));
  open(); check();
  await screen.findByText(new RegExp(label));
  expect(screen.queryByText("현재 미리보기 전송 조건")).toBeNull();
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  expect((screen.getByRole("button", { name: "Codex 실행 조건 다시 확인" }) as HTMLButtonElement).disabled).toBe(false);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(`${url}/codex-preview`);
});

it("hides the previous plan immediately during reload and after errors without exposing server details", async () => {
  let reject!: (error: Error) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(available).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; })).mockResolvedValue(available);
  open(); check(); await screen.findByText(`미리보기 SHA-256: ${available.preview_digest}`);
  refresh();
  expect(screen.queryByText("현재 미리보기 전송 조건")).toBeNull();
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  expect(screen.getByText("현재 Codex 실행 조건 확인 중")).toBeTruthy();
  await act(async () => reject(new Error("private-server-stack secret-env-value D:\\private\\account.json")));
  await screen.findByText(/Codex 실행 조건을 불러오지 못했습니다/);
  expect(screen.queryByText(/private-server-stack|secret-env-value|private.*account/)).toBeNull();
  expect(screen.queryByText("현재 미리보기 전송 조건")).toBeNull();
  refresh(); await screen.findByText(`미리보기 SHA-256: ${available.preview_digest}`);
  expect(screen.queryByText(/이전에 확인한 미리보기와 실행 조건이 달라졌습니다/)).toBeNull();
});

it("retains the last seen digest through an unavailable response and announces changed current conditions", async () => {
  const updated = { ...available, preview_digest: "3".repeat(64), manifest: { ...manifest, timeout_seconds: 45 } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(available).mockResolvedValueOnce(unavailable("codex_preview_configuration_changed")).mockResolvedValueOnce(updated).mockResolvedValue(updated);
  open(); check(); await screen.findByText(`미리보기 SHA-256: ${available.preview_digest}`);
  refresh(); await screen.findByText(/서버의 Codex 미리보기 설정이 바뀌었습니다/);
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  refresh(); await screen.findByText(/이전에 확인한 미리보기와 실행 조건이 달라졌습니다/);
  expect(screen.getByText(`미리보기 SHA-256: ${updated.preview_digest}`)).toBeTruthy();
  refresh(); await screen.findByText(`미리보기 SHA-256: ${updated.preview_digest}`);
  expect(screen.queryByText(/이전에 확인한 미리보기와 실행 조건이 달라졌습니다/)).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([path, options]) => path === `${url}/codex-preview` && options === undefined)).toBe(true);
});

it.each([
  { execution_id: "other" }, { actor_id: "other" }, { observed_sequence: 1 }, { execution_allowed: true },
  { schema_version: "UNKNOWN" }, { available: false, reason: "private-server-stack" },
  { manifest: { ...manifest, model: "wrong-model" } }, { binding: { ...available.binding, surface_id: "openai_api" } },
  { manifest: { ...manifest, argv_shape: "not-an-array" } }, { manifest: { ...manifest, argv_shape: ["D:\\private\\launcher.exe", "exec"] } },
  { manifest: { ...manifest, automatic_retry: true } }, { manifest: { ...manifest, timeout_seconds: -1 } },
])("rejects mismatched or malformed preview identity without stale display: %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...available, ...change });
  open(); check();
  await screen.findByText(/Codex 실행 조건을 확인하지 못했습니다/);
  expect(screen.queryByText("현재 미리보기 전송 조건")).toBeNull();
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  expect(screen.queryByText(/private-server-stack/)).toBeNull();
});

it("ignores repeated clicks while loading and a late result after unmount", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { unmount } = open(); check(); refresh();
  expect(api.fetchJson).toHaveBeenCalledTimes(1);
  expect((screen.getByRole("button", { name: "Codex 실행 조건 다시 확인" }) as HTMLButtonElement).disabled).toBe(true);
  unmount(); await act(async () => finish(available));
  expect(screen.queryByText("현재 미리보기 전송 조건")).toBeNull();
});

it("resets after sequence changes and ignores the old in-flight result", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue({ ...available, observed_sequence: 1 });
  const { rerender } = open(); check();
  rerender(<JournalCodexPreview url={url} saved={{ ...saved, current: { ...event, sequence: 1 } }}/>);
  expect(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" })).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledTimes(1);
  await act(async () => finish(available));
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  check(); await screen.findByText(`미리보기 SHA-256: ${available.preview_digest}`);
});

it("resets after topic or execution changes and does not fetch a new preview automatically", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(available);
  const { rerender } = open(); check(); await screen.findByText(/미리보기 SHA-256:/);
  rerender(<JournalCodexPreview url={`${url}-other`} saved={{ ...saved, contract: { ...saved.contract, execution_id: "other" } }}/>);
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
  expect(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" })).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledTimes(1);
});

it("does not show preview controls for other providers", () => {
  render(<JournalCodexPreview url={url} saved={{ ...saved, contract: { ...saved.contract, specification: { ...saved.contract.specification, provider_name: "xrml_offline_synthetic" } } }}/>);
  expect(screen.queryByRole("region", { name: "현재 Codex 실행 조건 미리보기" })).toBeNull();
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("never renders launcher paths or raw environment values from extra server metadata", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...available, launcher: "D:\\private\\launcher.exe", environment: { TOKEN: "do-not-display" }, manifest: { ...manifest, private_path: "D:\\private\\account.json" } });
  open(); check(); await screen.findByText(/미리보기 SHA-256:/);
  expect(screen.queryByText(/private|do-not-display/)).toBeNull();
});
