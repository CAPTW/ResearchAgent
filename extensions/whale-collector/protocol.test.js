import { test } from "node:test";
import assert from "node:assert/strict";
import { parseConnection, downloadPath, downloadObservation, nativeFailureCode, collectorErrorCode, collectorErrorMessage } from "./protocol.js";

test("ticket only targets exact numeric loopback with an opaque job capability", () => {
  const ticket = { base_url: "http://127.0.0.1:8765", job_id: "bc_fixture", token: "x".repeat(43) };
  assert.deepEqual(parseConnection(JSON.stringify(ticket)), ticket);
  for (const base_url of ["http://example.org:8765", "http://127.0.0.1:8765/path", "http://user@127.0.0.1:8765", "http://localhost:8765"]) {
    assert.throws(() => parseConnection(JSON.stringify({ ...ticket, base_url })));
  }
});
test("download filename is generated from bounded identities", () => {
  assert.equal(downloadPath("bc_fixture", "bci_fixture", "attempt"), "XRML/bc_fixture/bci_fixture/attempt.pdf");
  for (const item of ["../secret", "a:b", "a/b", ""]) assert.throws(() => downloadPath("job", item, "attempt"));
});
test("receipt binds extension, download ID and exact paper version", () => {
  const binding = { download_id: 7, pdf_url: "https://arxiv.org/pdf/2601.00001v1" };
  const item = { id: 7, byExtensionId: "extension", url: binding.pdf_url, finalUrl: binding.pdf_url, state: "complete" };
  assert.equal(downloadObservation(item, binding, "extension").id, 7);
  for (const invalid of [{ ...item, id: 8 }, { ...item, byExtensionId: "other" }, { ...item, finalUrl: "https://arxiv.org/login" }, { ...item, url: "https://arxiv.org/pdf/2601.00001v2" }]) {
    assert.throws(() => downloadObservation(invalid, binding, "extension"));
  }
});

test("native failures distinguish host lookup, access, exit, startup and protocol without inventing a cause", () => {
  const cases = [
    ["Specified native messaging host not found.", "native_host_not_found", /설치·등록·설정 파일/],
    ["Access to the specified native messaging host is forbidden.", "native_host_forbidden", /허용 확장 설정/],
    ["Native host has exited.", "native_host_exited", /응답 전에 종료/],
    ["Failed to start native messaging host.", "native_host_start_failed", /설치 상태와 실행 권한/],
    ["Error when communicating with the native messaging host.", "native_host_communication_failed", /메시지 형식 확인/],
  ];
  for (const [raw, code, guidance] of cases) {
    assert.equal(nativeFailureCode(new Error(raw)), code);
    assert.equal(collectorErrorCode(new Error(code)), code);
    assert.match(collectorErrorMessage(code), guidance);
    assert.equal(collectorErrorMessage(code).includes(raw), false);
  }
  const lookup = collectorErrorMessage("native_host_not_found");
  assert.match(lookup, /앱에 저장된 작업은 그대로/);
  assert.match(lookup, /실제 원인을 단정할 수 없습니다/);
  assert.match(lookup, /연결 코드 재발급만으로는 해결되지 않습니다/);
  assert.match(collectorErrorMessage("native_host_exited"), /종료 원인은 아직 확인되지 않았습니다/);
});

test("lost popup replies and unknown download outcomes require inspection, while expiry has separate guidance", () => {
  assert.match(collectorErrorMessage("collector_channel_unavailable"), /확장 화면과 백그라운드 사이/);
  assert.match(collectorErrorMessage("collector_channel_unavailable"), /설치 실패로 단정할 수 없습니다/);
  for (const code of ["collector_channel_unavailable", "native_host_exited", "download_dispatch_unknown_check_browser_history"]) {
    assert.match(collectorErrorMessage(code), /결과를 확인하기 전에는 새 다운로드를 시작하지 마세요/);
  }
  assert.match(collectorErrorMessage("download_dispatch_unknown_check_browser_history"), /명시적으로 선택/);
  assert.match(collectorErrorMessage("connection_expired_or_denied"), /만료·철회된 연결이면/);
});

test("unexpected errors never reflect raw input, paths or credential-shaped text", () => {
  const sensitive = 'synthetic-secret-token C:\\synthetic-private\\host.json {"token":"synthetic-secret-token"}';
  for (const error of [new Error(sensitive), sensitive, { message: sensitive }, null, undefined, 7,
    new Error(`Specified native messaging host not found. ${sensitive}`)]) {
    assert.equal(nativeFailureCode(error), "native_host_unavailable");
    assert.equal(collectorErrorCode(error), "collector_failed");
    assert.equal(collectorErrorMessage(error).includes("synthetic-secret-token"), false);
    assert.equal(collectorErrorMessage(error).includes("synthetic-private"), false);
    assert.match(collectorErrorMessage(error), /요청 결과를 확인하지 못했습니다/);
  }
});
