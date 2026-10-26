import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test as base, expect, topicA, installLedger, assertHygiene, type BrowserLedger } from "./m2-paper-bank.real-backend.helpers";

const test = base.extend<{ memoryLedger: BrowserLedger }>({
  memoryLedger: [async ({ backend, context, page }, use, info) => {
    const ledger = await installLedger(context, page);
    const runRoot = path.join(backend.dataRoot, "runs");
    const initialRuns = fs.readdirSync(runRoot).sort();
    await use(ledger);
    await assertHygiene(ledger);
    expect(ledger.requests.filter(r => /start-runs|\/actions|\/prototype\/.*run|\/retrieval/.test(r.pathname))).toEqual([]);
    expect(fs.readdirSync(runRoot).sort()).toEqual(initialRuns);
    backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: {
      providerCalls: 0, retrievalCalls: 0, automaticRuns: 0, runInventoryUnchanged: true,
      truthfulSnapshotsAndPersistence: info.status === "passed",
    }, timing: {}, hygiene: {consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length} });
  }, { auto: true }],
});

test("m3a-first-snapshot-empty-delta", async ({ page }) => {
  await page.goto(`/app/topics/${topicA}/insights`);
  const panel = page.getByRole("region", { name: "이전 스냅샷 이후 변경" });
  await expect(panel.getByText(/아직.*스냅샷/)).toBeVisible();
  await panel.getByRole("button", { name: "현재 상태 스냅샷 만들기" }).click();
  await expect(panel.getByText(/스냅샷이 하나/)).toBeVisible();
  await panel.getByRole("button", { name: "현재 상태 스냅샷 만들기" }).click();
  await expect(panel.getByText(/동일한 상태.*기존/)).toBeVisible();
  const response = await page.request.get(`/api/topics/${topicA}/memory/snapshots`);
  expect((await response.json()).total).toBe(1);
});

test("m3a-second-snapshot-deterministic-change-events", async ({ page }) => {
  await page.goto(`/app/topics/${topicA}/insights`);
  const panel = page.getByRole("region", { name: "이전 스냅샷 이후 변경" });
  await panel.getByRole("button", { name: "현재 상태 스냅샷 만들기" }).click();
  await expect(panel.getByText(/스냅샷이 하나/)).toBeVisible();
  const bank = await (await page.request.get(`/api/topics/${topicA}/paper-bank`)).json();
  const version = bank.items.find((item: {collectionIds: string[]}) => !item.collectionIds.includes("saved-papers")).paperVersionId;
  const save = await page.request.put(`/api/topics/${topicA}/paper-collections/saved-papers/members/${version}`);
  expect(save.ok()).toBe(true);
  await panel.getByRole("button", { name: "현재 상태 스냅샷 만들기" }).click();
  await expect(panel.getByRole("listitem")).toContainText("COLLECTION_MEMBERSHIP_CHANGED");
  await expect(panel.locator('[data-event-count="COLLECTION_MEMBERSHIP_CHANGED"]')).toHaveText("1");
  await expect(panel.locator('[data-event-count="PAPER_ADDED"]')).toHaveText("0");
  await expect(panel.getByRole("link", { name: `논문 버전 ${version}` })).toHaveAttribute("href", `/app/topics/${topicA}/papers?paper=${version}`);
  await panel.getByText("스냅샷 출처와 알고리즘", {exact:true}).click();
  await expect(panel.getByText("xrml-local-snapshot-diff-v1", {exact:true})).toBeVisible();
  const listing = await (await page.request.get(`/api/topics/${topicA}/memory/snapshots`)).json();
  expect(listing.total).toBe(2);
  await expect(panel.getByText(listing.items[0].snapshot_id, {exact:true})).toBeVisible();
  await expect(panel.getByText(listing.items[1].snapshot_id, {exact:true})).toBeVisible();
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await page.reload();
  await expect(panel.getByRole("listitem")).toContainText("COLLECTION_MEMBERSHIP_CHANGED");
});

const changedEvidenceTest = test.extend<{ changedEvidence: { before: string; after: string; comparison: any; insights: any } }>({
  changedEvidence: [async ({ backend }, use) => {
    const setup = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `
import json
from app.repositories import temporal_memory as m, temporal_sources as s, temporal_analysis as a, paper_reviews
from app.services import temporal_memory as memory_service, temporal_analysis as analysis_service
from tests.helpers.m2_paper_bank_fixture import RUN_A_2
t='${topicA}'
before,_=m.capture(t,lambda c:s.project(c,t))
review=paper_reviews.get_review(t,RUN_A_2,'2608.00001v2')
assert review is not None
span=review['evidence_spans'][0]
review['evidence_spans']=[{**span,'content_sha256':'a'*64,'text':'Captured synthetic evidence.'}]
paper_reviews.upsert_review(review)
after,_=m.capture(t,lambda c:s.project(c,t))
p,_=a.create_policy(t,{})
a.activate(t,p.policy_id)
a.evaluate(t,after.snapshot_id)
original={'before':before.snapshot_id,'after':after.snapshot_id,
 'comparison':memory_service.comparison(t,before.snapshot_id,after.snapshot_id,None),
 'insights':analysis_service.insights(t,25,1)}
review['evidence_spans']=[{**span,'content_sha256':'b'*64,'text':'Changed synthetic evidence under the same ID.'}]
paper_reviews.upsert_review(review)
print(json.dumps(original))
`], { cwd: process.env.XRML_M2_SOURCE_ROOT, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 60_000 });
    expect(setup.status, setup.error?.message || setup.stderr).toBe(0);
    await use(JSON.parse(setup.stdout));
  }, { timeout: 90_000 }],
});

changedEvidenceTest("m3a-historical-evidence-links-reject-changed-current-content", async ({ page, changedEvidence }) => {
  const evidenceId = "m2-evidence-v2-001";
  await page.goto(`/app/topics/${topicA}/insights`);
  const memory = page.getByRole("region", { name: "이전 스냅샷 이후 변경" });
  await expect(memory.getByText("EVIDENCE_CHANGED", { exact: true })).toBeVisible();
  await expect(memory.getByText(/현재 원문이 없거나 캡처 당시 내용과 달라/)).toContainText(evidenceId);
  await expect(memory.getByRole("link", { name: `근거 ${evidenceId}`, exact: true })).toHaveCount(0);
  await expect(memory.getByRole("link", { name: /논문 버전/ })).toHaveCount(1);
  await expect(memory.locator('[data-event-count="EVIDENCE_CHANGED"]')).toHaveText("1");
  const current = await (await page.request.get(`/api/topics/${topicA}/memory/diff?from=${changedEvidence.before}&to=${changedEvidence.after}`)).json();
  expect(current.counts).toEqual(changedEvidence.comparison.counts);
  const stableEvents = (items: any[]) => items.map(({ calculated_at, paper_destination, evidence_destinations, destination_reason, unavailable_evidence_ids, ...record }) => record);
  expect(stableEvents(current.events)).toEqual(stableEvents(changedEvidence.comparison.events));
  expect(changedEvidence.comparison.events[0].evidence_destinations).toEqual(expect.arrayContaining([expect.objectContaining({ id: evidenceId })]));
  const insights = await (await page.request.get(`/api/topics/${topicA}/analysis/insights`)).json();
  const stableInsights = (items: any[]) => items.map(({ paper_destinations, evidence_destinations, destination_reason, unavailable_evidence_ids, ...record }) => record);
  expect(stableInsights(insights.items)).toEqual(stableInsights(changedEvidence.insights.items));
  expect(insights.items.every((item: any) => item.unavailable_evidence_ids.includes(evidenceId))).toBe(true);
  expect(insights.items.every((item: any) => !item.evidence_destinations.some((link: any) => link.id === evidenceId))).toBe(true);
  await page.reload();
  await expect(memory.getByText(/현재 원문이 없거나 캡처 당시 내용과 달라/)).toBeVisible();
  await memory.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "historical-evidence-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(memory.getByText(/현재 원문이 없거나 캡처 당시 내용과 달라/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "historical-evidence-narrow.png"), fullPage: true });
});
