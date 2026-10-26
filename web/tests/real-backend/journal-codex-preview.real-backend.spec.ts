import fs from "node:fs";
import path from "node:path";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

function publicStrings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(publicStrings);
  if (value && typeof value === "object") return Object.values(value).flatMap(publicStrings);
  return [];
}

test("current Codex preview detects drift without a process, authority or saved attempt", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const configPath = process.env.XRML_CODEX_JOURNAL_PREVIEW_CONFIG!;
  expect(path.isAbsolute(configPath)).toBe(true);
  const configBytes = fs.readFileSync(configPath);
  const config = JSON.parse(configBytes.toString("utf8"));
  const v2 = config.schema_version === "CODEX_JOURNAL_PREVIEW_CONFIG_V2";
  const heading = v2 ? "Codex CLI V2 실행 계획" : "Codex CLI 실행 계획";
  const launcher = config.launcher[1] as string;
  const launcherBytes = fs.readFileSync(launcher);
  const marker = path.join(path.dirname(configPath), "unexpected-process.txt");
  const ledger = await installLedger(context, page);
  const runs = fs.readdirSync(path.join(backend.dataRoot, "runs")).sort();
  await page.goto(`/app/topics/${topicA}/settings`);
  const settings = page.getByRole("region", { name: "모델 연결 설정", exact: true });
  await settings.getByRole("textbox", { name: "Codex · ChatGPT 구독 모델", exact: true }).fill("fixture-preview-codex");
  await settings.getByRole("button", { name: "모델 연결 설정 저장", exact: true }).click();
  await expect(settings.getByText(/모델 연결 설정을 저장했습니다/)).toBeVisible();
  await page.goto(`/app/topics/${topicA}/papers`);
  await page.getByRole("table").locator("tbody input[type=checkbox]").first().check();
  await page.getByRole("button", { name: "선택 보고서 입력 고정" }).click();
  const panel = page.getByRole("region", { name: "선택 문헌 근거 초안" });
  await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
  const job = new URL(page.url()).searchParams.get("synthesis")!;
  const root = `/api/topics/${topicA}/local-synthesis/${job}`;
  for (let step = 1; step <= 3; step++) {
    await panel.getByRole("button", { name: "다음 한 단계 실행" }).click();
    await expect(panel.getByText(`${step === 3 ? "근거 초안 준비됨" : "수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
  }
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = panel.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByText("새 생성 요청 준비", { exact: true }).click();
  const form = results.getByRole("form", { name: "생성 요청 준비" });
  await expect(form.getByText("fixture-preview-codex", { exact: true })).toBeVisible();
  const preparedResponse = page.waitForResponse(r => r.url().endsWith(`${root}/journal-executions/selected-connection`) && r.request().method() === "POST");
  await form.getByRole("button", { name: "로컬 생성 요청 준비" }).click();
  const saved = (await (await preparedResponse).json()).execution;
  const detail = `${root}/journal-executions/${saved.contract.execution_id}`;
  const endpoint = `${detail}/codex-preview`;
  const preview = results.getByRole("region", { name: "현재 Codex 실행 조건 미리보기" });
  await expect(preview.getByRole("button", { name: "현재 Codex 실행 조건 확인", exact: true })).toBeVisible();
  expect(ledger.requests.filter(r => r.pathname.endsWith("/codex-preview"))).toHaveLength(0);
  const observed = page.waitForResponse(r => r.url().endsWith(endpoint));
  await preview.getByRole("button", { name: "현재 Codex 실행 조건 확인", exact: true }).press("Enter");
  const plan = await (await observed).json();
  fs.writeFileSync(info.outputPath("preview-readback.json"), JSON.stringify({ execution: saved, preview: plan }), "utf8");
  expect(plan).toMatchObject({ available: true, reason: null, preview_only: true, execution_allowed: false, observed_sequence: 0 });
  expect(plan.manifest.model).toBe("fixture-preview-codex");
  expect(plan.manifest.schema_version).toBe(v2 ? "CODEX_JOURNAL_CLI_V2" : "CODEX_JOURNAL_CLI_V1");
  expect(plan.binding.adapter_version).toBe(v2 ? "2" : "1");
  expect(plan.binding.command_shape_sha256).toMatch(/^[a-f0-9]{64}$/);
  for (const privateValue of [config.workspace, config.environment.CODEX_HOME, launcher]) {
    for (const value of publicStrings(plan)) expect(value).not.toContain(privateValue);
  }
  const compatibility = preview.getByRole("region", { name: "Codex CLI 호환성 기록" });
  const compatibilityEndpoint = `${detail}/codex-compatibility`;
  const readCompatibility = async () => {
    const response = page.waitForResponse(r => r.url().endsWith(compatibilityEndpoint));
    await compatibility.getByRole("button", { name: "CLI 설정 검사 기록 확인", exact: true }).press("Enter");
    return (await response).json();
  };
  const reportPath = process.env.XRML_CODEX_JOURNAL_COMPATIBILITY_REPORT;
  expect(ledger.requests.filter(r => r.pathname.endsWith("/codex-compatibility"))).toHaveLength(0);
  if (reportPath) {
    const reportBytes = fs.readFileSync(reportPath);
    const report = JSON.parse(reportBytes.toString("utf8"));
    // This is a deliberately fabricated browser fixture, not an installed CLI result.
    expect(report.cli_version).toBe("codex-cli 0.162.0-fixture");
    const observation = await readCompatibility();
    expect(observation).toMatchObject({ status: "CONFIGURATION_REJECTED", execution_allowed: false, model_invoked: false });
    await expect(compatibility).toContainText(`${v2 ? "V2" : "V1"} 설정을 적용한 로컬 CLI 검사에 실패했습니다`);
    await expect(compatibility).toContainText("검사 CLI: codex-cli 0.162.0-fixture");
    for (const value of publicStrings(observation)) expect(value).not.toContain(reportPath);
    await compatibility.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath("codex-compatibility-wide.png") });
    fs.writeFileSync(reportPath, "{broken");
    expect((await readCompatibility()).status).toBe("INVALID");
    await expect(compatibility.getByText(/검사 CLI:/)).toHaveCount(0);
    fs.writeFileSync(reportPath, JSON.stringify({ ...report, requirements_sha256: "f".repeat(64) }));
    expect((await readCompatibility()).status).toBe("STALE");
    if (v2) {
      fs.writeFileSync(reportPath, JSON.stringify({ ...report, schema_version: "CODEX_CLI_COMPATIBILITY_V1", profile: "CODEX_JOURNAL_CLI_V1" }));
      expect((await readCompatibility()).status).toBe("STALE");
    }
    fs.writeFileSync(reportPath, JSON.stringify({ ...report, status: "CONFIGURATION_OBSERVED", reason: null,
      checks: report.checks.map((check: { exit_code: number }) => ({ ...check, exit_code: 0 })) }));
    expect((await readCompatibility()).status).toBe("CONFIGURATION_OBSERVED");
    await expect(compatibility).toContainText("실제 exec 실행 호환성을 통과했다는 뜻은 아닙니다");
    fs.writeFileSync(reportPath, reportBytes);
    expect((await readCompatibility()).status).toBe("CONFIGURATION_REJECTED");
    expect((await page.request.get(compatibilityEndpoint.replace(topicA, topicB))).status()).toBe(404);
  } else {
    expect((await readCompatibility()).status).toBe("NOT_CHECKED");
  }
  await preview.getByText("현재 미리보기 전송 조건", { exact: true }).click();
  await expect(preview.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  expect(await preview.textContent()).toContain(plan.manifest.stdin_sha256);
  await preview.getByText("요청할 명령·제한 설정", { exact: true }).click();
  await expect(preview.getByLabel("Codex 명령 형태")).toContainText("--ignore-user-config");
  await expect(preview.getByLabel("Codex 명령 형태")).toContainText("--strict-config");
  await expect(preview.getByLabel("Codex 명령 형태")).toContainText("{explicit_launcher}");
  if (v2) {
    await expect(preview.getByLabel("Codex 명령 형태")).toContainText(JSON.stringify('model_provider="xrml_openai"'));
    await expect(preview.getByLabel("Codex 명령 형태")).toContainText(JSON.stringify('forced_login_method="chatgpt"'));
    await expect(preview.getByLabel("Codex 명령 형태")).not.toContainText("model_providers.openai.");
  }
  await preview.getByText("요청할 명령·제한 설정", { exact: true }).click();
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("codex-preview-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await preview.getByRole("heading", { name: heading, exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("codex-preview-narrow.png") });
  await compatibility.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("codex-compatibility-narrow.png") });
  expect(await (await page.request.get(detail)).json()).toEqual(saved);
  const refresh = preview.getByRole("button", { name: "Codex 실행 조건 다시 확인", exact: true });
  const refreshAndRead = async () => {
    const response = page.waitForResponse(r => r.url().endsWith(endpoint));
    await refresh.click(); return (await response).json();
  };
  // Changing preferences cannot retarget an already frozen Journal request.
  const settingsValue = await (await page.request.get("/settings/model-connections")).json();
  expect((await page.request.put("/settings/model-connections", { data: {
    expected_revision: settingsValue.revision, default_connector_id: "openai_api", models: { openai_api: "fixture-other-model" },
  } })).status()).toBe(200);
  expect((await refreshAndRead()).preview_digest).toBe(plan.preview_digest);
  // Configuration drift cannot become a silently substituted plan, even if restored.
  fs.writeFileSync(configPath, JSON.stringify({ ...config, timeout_seconds: 31 }));
  expect((await refreshAndRead()).reason).toBe("codex_preview_configuration_changed");
  await expect(preview.getByText("현재 미리보기 전송 조건", { exact: true })).toHaveCount(0);
  await expect(preview).toContainText("재시작");
  fs.writeFileSync(configPath, configBytes);
  expect((await refreshAndRead()).reason).toBe("codex_preview_configuration_changed");
  await backend.restart();
  expect((await refreshAndRead()).preview_digest).toBe(plan.preview_digest);
  expect((await readCompatibility()).status).toBe(reportPath ? "CONFIGURATION_REJECTED" : "NOT_CHECKED");
  // Runtime fingerprints bind the real fake-launcher bytes, without invoking it.
  fs.appendFileSync(launcher, "\n# changed fixture bytes\n");
  expect((await refreshAndRead()).reason).toBe("codex_preview_runtime_changed");
  await expect(preview.getByText("현재 미리보기 전송 조건", { exact: true })).toHaveCount(0);
  await backend.restart();
  const replaced = await refreshAndRead();
  expect(replaced.available).toBe(true); expect(replaced.preview_digest).not.toBe(plan.preview_digest);
  expect(replaced.manifest.runtime_identity.launcher_sha256).not.toBe(plan.manifest.runtime_identity.launcher_sha256);
  await expect(preview).toContainText("변경");
  expect((await readCompatibility()).status).toBe(reportPath ? "STALE" : "NOT_CHECKED");
  expect(await (await page.request.get(detail)).json()).toEqual(saved);
  expect((await page.request.get(endpoint.replace(topicA, topicB))).status()).toBe(404);
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  await history.getByRole("button", { name: "이 요청의 결과 저장 취소" }).click();
  await expect(history.getByText(/최근 저장 상태: 결과 저장 취소 · 시도 0/)).toBeVisible();
  await expect(preview.getByText("현재 미리보기 전송 조건", { exact: true })).toHaveCount(0);
  await expect(preview.getByRole("button", { name: "현재 Codex 실행 조건 확인", exact: true })).toBeVisible();
  const cancelled = await (await page.request.get(detail)).json();
  expect(cancelled.contract).toEqual(saved.contract); expect(cancelled.events).toHaveLength(1);
  expect(fs.existsSync(marker)).toBe(false); expect(fs.existsSync(config.workspace)).toBe(false);
  expect(fs.readdirSync(config.environment.CODEX_HOME)).toEqual([]);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  await assertHygiene(ledger);
  fs.writeFileSync(launcher, launcherBytes);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitReadOnlyPreview: true, driftLatched: true, restartReconfigured: true, noRuntimeInvocation: true, noAuthorityOrAttempt: true, noPrivatePaths: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
