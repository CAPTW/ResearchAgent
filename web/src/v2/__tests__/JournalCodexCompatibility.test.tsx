import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalCodexCompatibility } from "../views/JournalCodexCompatibility";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const props = { url: "/execution", executionId: "exec", actorId: "owner", sequence: 0, contractDigest: "a".repeat(64),
  runtimeIdentity: { launcher_sha256: "b".repeat(64), environment_sha256: "c".repeat(64), workspace_sha256: "d".repeat(64) } };
const empty = { schema_version: "CODEX_JOURNAL_COMPATIBILITY_V1", execution_id: "exec", actor_id: "owner", observed_sequence: 0,
  contract_digest: props.contractDigest, execution_allowed: false, model_invoked: false, status: "NOT_CHECKED", reason: "report_not_configured", report: null };
beforeEach(() => vi.resetAllMocks());
const report = { schema_version: "CODEX_CLI_COMPATIBILITY_V1", profile: "CODEX_JOURNAL_CLI_V1", runtime_identity: props.runtimeIdentity,
  requirements_sha256: "e".repeat(64), checked_at: "2026-10-07T00:00:00Z", cli_version: "codex-cli 0.162.0-alpha.2",
  status: "CONFIGURATION_REJECTED", reason: "config_rejected", execution_allowed: false, model_invoked: false,
  checks: ["version", "exec_help", "configuration"].map(name => ({ name, exit_code: name === "configuration" ? 1 : 0, stdout_sha256: "a".repeat(64) })) };
const rejected = { ...empty, status: report.status, reason: "config_rejected", report };
function click() { fireEvent.click(screen.getByRole("button", { name: "CLI 설정 검사 기록 확인" })); }

it("reads an existing observation only after an explicit click", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(empty);
  render(<JournalCodexCompatibility {...props}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "CLI 설정 검사 기록 확인" }));
  await screen.findByText(/서버에 연결된 로컬 검사 기록이 없습니다/);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/execution/codex-compatibility");
  expect(screen.getByText(/이 버튼은 저장된 검사 기록만 읽으며/)).toBeTruthy();
});

it.each([
  ["CONFIGURATION_REJECTED", "V1 설정을 적용한 로컬 CLI 검사에 실패했습니다"],
  ["CONFIGURATION_OBSERVED", "실제 exec 실행 호환성을 통과했다는 뜻은 아닙니다"],
  ["INCOMPLETE", "필요한 옵션을 확인하지 못했습니다"],
])("labels %s without granting permission", async (status, message) => {
  const reason = status === "CONFIGURATION_OBSERVED" ? null : status === "INCOMPLETE" ? "probe_failed" : report.reason;
  const checks = status === "CONFIGURATION_OBSERVED" ? report.checks.map(check => ({ ...check, exit_code: 0 }))
    : status === "INCOMPLETE" ? report.checks.slice(0, 2) : report.checks;
  vi.mocked(api.fetchJson).mockResolvedValue({ ...rejected, status, reason, report: { ...report, status, reason, checks } });
  render(<JournalCodexCompatibility {...props}/>); click();
  await screen.findByText(new RegExp(message));
  expect(screen.getByText(/검사 CLI: codex-cli 0.162.0-alpha.2/)).toBeTruthy();
  expect(screen.getByText(/별도 빈 프로필/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /실행 시작|승인/ })).toBeNull();
});

it.each([["STALE", "현재와 다릅니다"], ["INVALID", "읽거나 검증할 수 없습니다"], ["UNAVAILABLE", "실행 환경을 확인할 수 없습니다"]])(
  "hides old observation after %s", async (status, message) => {
    vi.mocked(api.fetchJson).mockResolvedValueOnce(rejected).mockResolvedValue({ ...empty, status });
    render(<JournalCodexCompatibility {...props}/>); click(); await screen.findByText(/검사 CLI:/);
    click(); expect(screen.queryByText(/검사 CLI:/)).toBeNull(); await screen.findByText(new RegExp(message));
  });

it.each([
  { execution_id: "other" }, { actor_id: "other" }, { observed_sequence: 1 }, { contract_digest: "z".repeat(64) },
  { execution_allowed: true }, { model_invoked: true }, { status: "APPROVED" },
  { report: { ...report, execution_allowed: true } }, { report: { ...report, profile: "future" } },
  { report: { ...report, cli_version: "private error and file path" } }, { report: { ...report, checked_at: "bad date" } },
  { report: { ...report, checked_at: "2026-10-07T00:00:00" } }, { report: { ...report, checks: [] } },
  { report: { ...report, checks: report.checks.map(check => ({ ...check, exit_code: 0 })) } },
  { status: "CONFIGURATION_OBSERVED", report: { ...report, status: "CONFIGURATION_OBSERVED" } },
  { report: { ...report, runtime_identity: { ...props.runtimeIdentity, launcher_sha256: "f".repeat(64) } } },
])("rejects mismatched or misleading response %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...rejected, ...change });
  render(<JournalCodexCompatibility {...props}/>); click();
  await screen.findByRole("alert"); expect(screen.queryByText(/검사 CLI:/)).toBeNull();
  expect(screen.queryByText(/private error/)).toBeNull();
});

it("keeps retries explicit and drops late replies after switching requests", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const view = render(<JournalCodexCompatibility {...props}/>); click(); click();
  expect(api.fetchJson).toHaveBeenCalledTimes(1);
  view.rerender(<JournalCodexCompatibility {...props} url="/other" executionId="other"/>);
  await act(async () => { resolve(rejected); });
  expect(screen.queryByText(/검사 CLI:/)).toBeNull();
  expect(screen.queryByText(/V1 설정을 적용한 로컬 CLI 검사에 실패했습니다/)).toBeNull();
});

it("redacts read errors and retries without leaving the old result visible", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(rejected).mockRejectedValueOnce(new Error("PRIVATE SECRET D:\\path"))
    .mockResolvedValue(empty);
  render(<JournalCodexCompatibility {...props}/>); click(); await screen.findByText(/검사 CLI:/);
  click(); await screen.findByRole("alert");
  expect(screen.queryByText(/검사 CLI:/)).toBeNull(); expect(screen.queryByText(/PRIVATE SECRET/)).toBeNull();
  click(); await screen.findByText(/서버에 연결된 로컬 검사 기록이 없습니다/);
  expect(api.fetchJson).toHaveBeenCalledTimes(3);
});
