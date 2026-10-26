import {
  expect,
  test as base,
  type BrowserContext,
  type Page,
  type Request,
  type Response,
  type TestInfo,
} from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

export const appOrigin = "http://127.0.0.1:8002";
export const topicA = "m2-spatial-paper-bank";
export const topicB = "m2-robotics-paper-bank";
export const approvalFixtureSessionSecret = "synthetic-browser-approval-session-20261008-not-a-real-secret";
export const windowsAbsolutePath = /(?:^|["'\s(])[A-Za-z]:[\\/]/;

export type RequestRecord = { method: string; origin: string; pathname: string; search: string };
export type RequestTiming = RequestRecord & {
  startedAtMs: number;
  finishedAtMs: number | null;
  status: number | null;
};
export type RemainingBudget = () => number;
export type PapersNavigationObservation = {
  navigationStartedAtMs: number;
  routeVisibleMs: number;
  initialTruthVisibleMs: number;
  initialTruthText: string;
  paperBankResponse: Response;
  collectionsResponse: Response | null;
  detailResponse: Response | null;
  paperBankResponseMs: number;
};
export type BrowserLedger = {
  requests: RequestRecord[];
  requestTimings: RequestTiming[];
  consoleErrors: string[];
  pageErrors: string[];
  offOriginAttempts: string[];
  mutationRequests: string[];
  handledExpectedErrors: string[];
};

export type PaperBankItem = {
  title: string;
  canonicalPaperId: string;
  paperVersionId: string;
  collectionIds: string[];
  sourceIdentity: { nativeId: string | null };
  compatibility: { paperId: string; occurrences: unknown[] };
  authors: string[];
  review: { state: string; evidenceCount: number };
};

export type PaperBankPayload = {
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  queryDigest: string;
  items: PaperBankItem[];
};

type JourneyResult = {
  journey: string;
  migrationIds: string[];
  assertions: Record<string, boolean | number | string>;
  timing: Record<string, number | string | boolean | null>;
  hygiene: {
    consoleErrors: number;
    pageErrors: number;
    offOriginAttempts: number;
  };
};

function requireEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(name + " must be configured by the D-drive harness");
  return value;
}

export function requireAbsoluteDDrive(value: string, label: string): string {
  if (!path.isAbsolute(value)) throw new Error(label + " must be absolute");
  if (path.parse(path.resolve(value)).root.toUpperCase() !== "D:\\") {
    throw new Error(label + " must resolve to D:");
  }
  return path.resolve(value);
}

function safeSlug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/gu, "-").replace(/^-|-$/gu, "").slice(0, 48);
}

function portOpen(): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port: 8002 });
    const finish = (value: boolean) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(value);
    };
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
    socket.setTimeout(500, () => finish(false));
  });
}

async function waitForReady(timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(appOrigin + "/health/ready", {
        signal: AbortSignal.timeout(Math.min(750, Math.max(1, deadline - Date.now()))),
      });
      if (response.ok) return;
    } catch {
      // Readiness polling is lifecycle control, not semantic synchronization.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("owned FastAPI backend did not become ready");
}

async function waitForPortClosed(timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await portOpen())) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("owned FastAPI listener remained open after shutdown");
}

export class OwnedBackend {
  readonly root: string;
  readonly dataRoot: string;
  readonly tempRoot: string;
  readonly sourceRoot: string;
  readonly pythonExe: string;
  readonly launchEvents: Array<{ launch: number; readyMs: number; pid: number }> = [];
  private child: ChildProcess | null = null;
  private launchCount = 0;

  private constructor(root: string, sourceRoot: string, pythonExe: string, private codexDispatchFixture = false, private paperCodexFixture = false, private codexApprovalFixture = false, private capturedAnalysisFixture = false, private anthropicNativeFixture = false) {
    this.root = root;
    this.tempRoot = path.join(root, "temp");
    this.dataRoot = path.join(this.tempRoot, "runtime-data");
    this.sourceRoot = sourceRoot;
    this.pythonExe = pythonExe;
  }

  static async create(testInfo: TestInfo, codexDispatchFixture = false, paperCodexFixture = false, codexApprovalFixture = false, capturedAnalysisFixture = false, anthropicNativeFixture = false): Promise<OwnedBackend> {
    if ([codexDispatchFixture, paperCodexFixture, codexApprovalFixture, capturedAnalysisFixture, anthropicNativeFixture].filter(Boolean).length > 1) throw new Error("Select one explicit synthetic fixture");
    const suiteRoot = requireAbsoluteDDrive(requireEnv("XRML_M2_SUITE_RUNTIME_ROOT"), "XRML_M2_SUITE_RUNTIME_ROOT");
    const sourceRoot = requireAbsoluteDDrive(requireEnv("XRML_M2_SOURCE_ROOT"), "XRML_M2_SOURCE_ROOT");
    const pythonExe = requireAbsoluteDDrive(requireEnv("XRML_M2_PYTHON_EXE"), "XRML_M2_PYTHON_EXE");
    const configuredPythonCache = process.env.XRML_M2_PYTHON_CACHE_ROOT?.trim();
    const pythonCacheRoot = configuredPythonCache
      ? requireAbsoluteDDrive(configuredPythonCache, "XRML_M2_PYTHON_CACHE_ROOT")
      : null;
    const root = path.join(
      suiteRoot,
      safeSlug(testInfo.title) + "-" + testInfo.parallelIndex + "-" + testInfo.retry,
    );
    if (fs.existsSync(root)) throw new Error("test runtime root already exists: " + root);
    fs.mkdirSync(path.join(root, "temp"), { recursive: true });
    fs.mkdirSync(path.join(root, "python-cache"), { recursive: true });
    if (pythonCacheRoot) fs.mkdirSync(pythonCacheRoot, { recursive: true });
    fs.mkdirSync(path.join(root, "logs"), { recursive: true });
    fs.mkdirSync(path.join(root, "temp", "runtime-data"), { recursive: true });

    const backend = new OwnedBackend(root, sourceRoot, pythonExe, codexDispatchFixture, paperCodexFixture, codexApprovalFixture, capturedAnalysisFixture, anthropicNativeFixture);
    backend.seed();
    await backend.start();
    return backend;
  }

  private environment(): NodeJS.ProcessEnv {
    const configuredPythonCache = process.env.XRML_M2_PYTHON_CACHE_ROOT?.trim();
    const pythonCacheRoot = configuredPythonCache
      ? requireAbsoluteDDrive(configuredPythonCache, "XRML_M2_PYTHON_CACHE_ROOT")
      : path.join(this.root, "python-cache");
    return {
      ...process.env,
      TEMP: this.tempRoot,
      TMP: this.tempRoot,
      TMPDIR: this.tempRoot,
      PYTHONPYCACHEPREFIX: pythonCacheRoot,
      XRML_DATA_DIR: this.dataRoot,
      XRML_RUN_EXECUTOR_MODE: "disabled",
      ...(process.env.XRML_JOURNAL_OFFLINE_ENABLED === "1" ? {
        XRML_ENVIRONMENT: "development",
        XRML_JOURNAL_OFFLINE_DATA_ROOT: this.dataRoot,
        XRML_SHARED_DEPLOYMENT: "0",
        APP_BASE_URL: appOrigin,
      } : {}),
      ...(process.env.XRML_REVIEW_OFFLINE_ENABLED === "1" ? {
        XRML_ENVIRONMENT: "development",
        XRML_REVIEW_OFFLINE_DATA_ROOT: this.dataRoot,
        XRML_SHARED_DEPLOYMENT: "0",
        APP_BASE_URL: appOrigin,
      } : {}),
      ...(process.env.XRML_REVIEW_EXECUTION_OFFLINE_ENABLED === "1" ? {
        XRML_ENVIRONMENT: "development",
        XRML_REVIEW_EXECUTION_OFFLINE_DATA_ROOT: this.dataRoot,
        XRML_SHARED_DEPLOYMENT: "0",
        APP_BASE_URL: appOrigin,
      } : {}),
      ...(process.env.XRML_PAPER_TRANSPORT_OFFLINE_ENABLED === "1" ? {
        XRML_ENVIRONMENT: "development",
        XRML_PAPER_TRANSPORT_OFFLINE_DATA_ROOT: this.dataRoot,
        XRML_SHARED_DEPLOYMENT: "0",
        APP_BASE_URL: appOrigin,
      } : {}),
      ...(process.env.XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED === "1" ? {
        XRML_ENVIRONMENT: "development",
        XRML_PAPER_ANTHROPIC_OFFLINE_DATA_ROOT: this.dataRoot,
        XRML_SHARED_DEPLOYMENT: "0",
        APP_BASE_URL: appOrigin,
      } : {}),
      AUTH_MODE: this.codexApprovalFixture ? "oidc" : "disabled",
      ...(this.codexApprovalFixture ? {
        XRML_CODEX_APPROVAL_FIXTURE: "1", XRML_ENVIRONMENT: "development", XRML_SHARED_DEPLOYMENT: "0",
        SESSION_SECRET: approvalFixtureSessionSecret, APP_BASE_URL: appOrigin, COOKIE_SECURE: "0",
        OIDC_ISSUER: "", OIDC_CLIENT_ID: "", OIDC_CLIENT_SECRET: "", OIDC_AUDIENCE: "",
        ADMIN_USER_IDS: "", ADMIN_USER_EMAILS: "",
      } : {}),
      TOPIC_SCHEDULER_ENABLED: "0",
      WEB_APP_BASE_PATH: "/app",
    };
  }

  private seed(): void {
    const fixtureScript = path.join(this.sourceRoot, "tests", "helpers", "m2_paper_bank_fixture.py");
    const result = spawnSync(this.pythonExe, [fixtureScript, "--root", this.dataRoot], {
      cwd: this.sourceRoot,
      // Existing seed services use the explicit local actor. The new browser
      // session/server switches to OIDC only after disposable fixture creation.
      env: this.codexApprovalFixture ? { ...this.environment(), AUTH_MODE: "disabled" } : this.environment(),
      encoding: "utf8",
      timeout: 60_000,
      windowsHide: true,
    });
    if (result.status !== 0) {
      throw new Error(`fixture seed failed (status=${result.status}, signal=${result.signal}): `
        + (result.error?.message || result.stderr || result.stdout));
    }
    fs.writeFileSync(path.join(this.root, "fixture-result.json"), result.stdout, "utf8");
  }

  async start(): Promise<void> {
    if (this.child) throw new Error("owned backend is already running");
    if (await portOpen()) throw new Error("port 8002 is occupied before owned backend start");
    this.launchCount += 1;
    const stdoutFd = fs.openSync(path.join(this.root, "logs", "backend-" + this.launchCount + ".stdout.log"), "a");
    const stderrFd = fs.openSync(path.join(this.root, "logs", "backend-" + this.launchCount + ".stderr.log"), "a");
    const startedAt = Date.now();
    const child = spawn(
      this.pythonExe,
      ["-m", "uvicorn", ...(this.anthropicNativeFixture
        ? ["tests.helpers.anthropic_native_app_fixture:create_app", "--factory"] : this.capturedAnalysisFixture
        ? ["tests.helpers.captured_analysis_app_fixture:create_app", "--factory"] : this.codexApprovalFixture
        ? ["tests.helpers.codex_approval_app_fixture:create_app", "--factory"] : this.paperCodexFixture
        ? ["tests.helpers.paper_codex_app_fixture:create_app", "--factory"] : this.codexDispatchFixture
          ? ["tests.helpers.journal_codex_app_fixture:create_app", "--factory"] : ["app.main:app"]),
        "--host", "127.0.0.1", "--port", "8002", "--log-level", "info"],
      {
        cwd: this.sourceRoot,
        env: this.environment(),
        stdio: ["ignore", stdoutFd, stderrFd],
        windowsHide: true,
      },
    );
    fs.closeSync(stdoutFd);
    fs.closeSync(stderrFd);
    this.child = child;
    try {
      // Cold Python imports alone exceeded 15 s in the D-drive fixture logs.
      // This is fixture startup; browser journeys retain their 45 s budget.
      await waitForReady(30_000);
    } catch (error) {
      await this.stop();
      throw error;
    }
    if (!child.pid) throw new Error("owned backend has no process ID");
    this.launchEvents.push({ launch: this.launchCount, readyMs: Date.now() - startedAt, pid: child.pid });
  }

  async stop(): Promise<void> {
    const child = this.child;
    this.child = null;
    if (!child?.pid) {
      await waitForPortClosed(2_000);
      return;
    }
    if (process.platform === "win32") {
      const result = spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
        encoding: "utf8",
        timeout: 10_000,
        windowsHide: true,
      });
      if (result.status !== 0 && await portOpen()) {
        throw new Error("failed to stop owned backend tree " + child.pid + ": " + (result.stderr || result.stdout));
      }
    } else if (child.exitCode === null) {
      child.kill("SIGTERM");
    }
    await waitForPortClosed(10_000);
  }

  async restart(): Promise<void> {
    await this.stop();
    await this.start();
  }

  disposeFixtureData(): void {
    const startedAt = Date.now();
    const expectedDataRoot = path.resolve(this.root, "temp", "runtime-data");
    const backendStoppedBeforeDisposal = this.child === null;
    let error: string | null = null;
    let dataRootRemoved = false;
    try {
      if (!backendStoppedBeforeDisposal) {
        throw new Error("owned backend must be stopped before fixture-data disposal");
      }
      if (path.resolve(this.dataRoot) !== expectedDataRoot) {
        throw new Error("fixture data root does not match the owned runtime-data path");
      }
      fs.rmSync(this.dataRoot, { recursive: true, force: false, maxRetries: 2, retryDelay: 100 });
      dataRootRemoved = !fs.existsSync(this.dataRoot);
      if (!dataRootRemoved) throw new Error("owned fixture data root remained after disposal");
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
    fs.writeFileSync(
      path.join(this.root, "cleanup-result.json"),
      JSON.stringify({
        ownership: "test-owned-runtime-data",
        backendStoppedBeforeDisposal,
        dataRootRemoved,
        durationMs: Date.now() - startedAt,
        error,
      }, null, 2) + "\n",
      "utf8",
    );
    if (error) throw new Error("fixture-data cleanup failed: " + error);
  }

  writeJourneyResult(result: JourneyResult): void {
    fs.writeFileSync(
      path.join(this.root, "journey-result.json"),
      JSON.stringify({ ...result, launchEvents: this.launchEvents }, null, 2) + "\n",
      "utf8",
    );
  }
}

export const test = base.extend<{ backend: OwnedBackend; codexDispatchFixture: boolean; paperCodexFixture: boolean; codexApprovalFixture: boolean; capturedAnalysisFixture: boolean; anthropicNativeFixture: boolean }>({
  codexDispatchFixture: [false, { option: true }],
  paperCodexFixture: [false, { option: true }],
  codexApprovalFixture: [false, { option: true }],
  capturedAnalysisFixture: [false, { option: true }],
  anthropicNativeFixture: [false, { option: true }],
  backend: [async ({ codexDispatchFixture, paperCodexFixture, codexApprovalFixture, capturedAnalysisFixture, anthropicNativeFixture }, use, testInfo) => {
    const backend = await OwnedBackend.create(testInfo, codexDispatchFixture, paperCodexFixture, codexApprovalFixture, capturedAnalysisFixture, anthropicNativeFixture);
    try {
      await use(backend);
    } finally {
      await backend.stop();
      backend.disposeFixtureData();
    }
  // Synthetic DB creation performs real durable writes. Its setup/teardown
  // budget is separate from each journey's 45 s product interaction budget.
  }, { auto: true, timeout: 120_000 }],
});

export { expect };

export function remainingBudget(testInfo: TestInfo, startedAt = Date.now()): RemainingBudget {
  const deadline = startedAt + testInfo.timeout;
  return () => Math.max(1, deadline - Date.now());
}

export function withinDeadline(remaining: RemainingBudget) {
  return expect.configure({ timeout: remaining() });
}

function requestRecord(request: Request): RequestRecord {
  const url = new URL(request.url());
  return { method: request.method(), origin: url.origin, pathname: url.pathname, search: url.search };
}

export function matchesApiResponse(response: Response, pathname: string, method = "GET") {
  const url = new URL(response.url());
  return response.request().method() === method && url.pathname === pathname;
}

export function armPaperBankResponse(page: Page, topicId: string, remaining: RemainingBudget) {
  return page.waitForResponse(
    (response) => matchesApiResponse(response, "/api/topics/" + topicId + "/paper-bank"),
    { timeout: remaining() },
  );
}

export function armCollectionsResponse(page: Page, topicId: string, remaining: RemainingBudget) {
  return page.waitForResponse(
    (response) => matchesApiResponse(response, "/api/topics/" + topicId + "/paper-collections"),
    { timeout: remaining() },
  );
}

export function armDetailResponse(
  page: Page,
  topicId: string,
  paperVersionId: string,
  remaining: RemainingBudget,
) {
  return page.waitForResponse(
    (response) => matchesApiResponse(
      response,
      "/api/topics/" + topicId + "/paper-bank/" + encodeURIComponent(paperVersionId),
    ),
    { timeout: remaining() },
  );
}

export async function assertImmediatePapersTruth(page: Page, remaining: RemainingBudget) {
  await withinDeadline(remaining)(page.locator(".v2-app-shell")).toBeVisible();
  const papersRoute = page.getByRole("region", { name: "논문 목록 화면" });
  await withinDeadline(remaining)(papersRoute).toBeVisible();
  const bodyText = (await page.locator("body").innerText({ timeout: remaining() })).trim();
  expect(bodyText.length).toBeGreaterThan(0);
  const truthfulState = papersRoute.locator(
    '.v2-state-card[aria-busy="true"], .v2-state-card[role="alert"], .v2-state-card.is-unavailable, .v2-empty, table[aria-label="논문 목록"]',
  ).first();
  await withinDeadline(remaining)(truthfulState).toBeVisible();
  const truthfulStateText = (await truthfulState.textContent({ timeout: remaining() }))?.trim() ?? "";
  expect(truthfulStateText.length).toBeGreaterThan(0);
  return truthfulStateText;
}

export async function navigateToPapers(
  page: Page,
  url: string,
  topicId: string,
  remaining: RemainingBudget,
  options: {
    expectedPaperBankStatus?: number;
    observeCollections?: boolean;
    detailPaperVersionId?: string;
    expectedDetailStatus?: number;
  } = {},
): Promise<PapersNavigationObservation> {
  const navigationStartedAtMs = Date.now();
  const paperBankPromise = armPaperBankResponse(page, topicId, remaining);
  const collectionsPromise = options.observeCollections === false
    ? null
    : armCollectionsResponse(page, topicId, remaining);
  const detailPromise = options.detailPaperVersionId
    ? armDetailResponse(page, topicId, options.detailPaperVersionId, remaining)
    : null;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: remaining() });
  const routeVisibleMs = Date.now() - navigationStartedAtMs;
  const initialTruthText = await assertImmediatePapersTruth(page, remaining);
  const initialTruthVisibleMs = Date.now() - navigationStartedAtMs;
  const paperBankResponse = await paperBankPromise;
  const paperBankResponseMs = Date.now() - navigationStartedAtMs;
  expect(paperBankResponse.status()).toBe(options.expectedPaperBankStatus ?? 200);
  const collectionsResponse = collectionsPromise ? await collectionsPromise : null;
  if (collectionsResponse) expect(collectionsResponse.ok()).toBe(true);
  const detailResponse = detailPromise ? await detailPromise : null;
  if (detailResponse) expect(detailResponse.status()).toBe(options.expectedDetailStatus ?? 200);
  return {
    navigationStartedAtMs,
    routeVisibleMs,
    initialTruthVisibleMs,
    initialTruthText,
    paperBankResponse,
    collectionsResponse,
    detailResponse,
    paperBankResponseMs,
  };
}

export async function installLedger(context: BrowserContext, page: Page): Promise<BrowserLedger> {
  const ledger: BrowserLedger = {
    requests: [],
    requestTimings: [],
    consoleErrors: [],
    pageErrors: [],
    offOriginAttempts: [],
    mutationRequests: [],
    handledExpectedErrors: [],
  };
  const timingByRequest = new Map<Request, RequestTiming>();
  await context.route(/^https?:\/\//, async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== appOrigin) {
      ledger.offOriginAttempts.push(url.origin + url.pathname);
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  page.on("console", (message) => {
    if (message.type() === "error") ledger.consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => ledger.pageErrors.push(error.message));
  page.on("request", (request) => {
    const record = requestRecord(request);
    ledger.requests.push(record);
    const timing: RequestTiming = { ...record, startedAtMs: Date.now(), finishedAtMs: null, status: null };
    timingByRequest.set(request, timing);
    ledger.requestTimings.push(timing);
    if (["POST", "PUT", "PATCH", "DELETE"].includes(record.method)) {
      ledger.mutationRequests.push(record.method + " " + record.pathname);
    }
  });
  page.on("response", (response) => {
    const timing = timingByRequest.get(response.request());
    if (!timing) return;
    timing.finishedAtMs = Date.now();
    timing.status = response.status();
  });
  page.on("requestfailed", (request) => {
    const timing = timingByRequest.get(request);
    if (timing) timing.finishedAtMs = Date.now();
  });
  return ledger;
}

export async function getPaperBank(page: Page, query = "pageSize=100"): Promise<PaperBankPayload> {
  const response = await page.request.get("/api/topics/" + topicA + "/paper-bank?" + query);
  expect(response.ok()).toBe(true);
  return await response.json() as PaperBankPayload;
}

export function versionTwoFrom(bank: PaperBankPayload): PaperBankItem {
  const item = bank.items.find((candidate) => candidate.sourceIdentity.nativeId === "2608.00001v2");
  expect(item).toBeTruthy();
  return item!;
}

export async function assertHygiene(ledger: BrowserLedger): Promise<void> {
  expect(ledger.offOriginAttempts).toEqual([]);
  expect(ledger.consoleErrors).toEqual([]);
  expect(ledger.pageErrors).toEqual([]);
}

export function rewriteFirstOccurrenceId(source: string, occurrenceId: unknown) {
  const lines = source.split(/\r?\n/u);
  const index = lines.findIndex((line) => line.trim().length > 0);
  if (index < 0) throw new Error("triage.jsonl must contain at least one record");
  const record = JSON.parse(lines[index]) as Record<string, unknown>;
  record.occurrence_id = occurrenceId;
  lines[index] = JSON.stringify(record);
  return lines.join("\n");
}

export function migrationIds(...ranges: Array<[number, number]>): string[] {
  return ranges.flatMap(([start, end]) =>
    Array.from({ length: end - start + 1 }, (_, index) => "M2RB-" + String(start + index).padStart(3, "0"))
  );
}
