import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("recommendation versions preserve decisions across changed output and restart",async({page,backend,context},info)=>{
 test.setTimeout(130000); // Two bounded UI sessions plus owned cold restart.
 const ledger=await installLedger(context,page);
 const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 const root=`/api/topics/${topicA}/recommendation-versions`;
 const panel=page.getByRole("region",{name:"추천 버전과 검토 이력"});
 const {versions,decisions,firstId}=await test.step("save, review and change current output",async()=>{
  await page.goto(`/app/topics/${topicA}/insights`);
  await expect(panel.getByText("저장된 추천 버전이 없습니다.")).toBeVisible();
  const current=await(await page.request.get(`/api/topics/${topicA}/recommendations`)).json();
  expect(current.items.length).toBeGreaterThan(0);
  const item=current.items[0];
  const summary=panel.locator("summary").filter({hasText:item.title}).first();
  await summary.click();
  await panel.getByRole("button",{name:"이 버전 저장"}).first().click();
  await expect(panel.getByText("1개 저장 버전",{exact:true})).toBeVisible();
  const original=(await(await page.request.get(root)).json()).items[0];
  expect(original.version_id).toBe(item.versionId);
  const saved=panel.getByRole("article",{name:`저장된 추천: ${item.title}`});
  await saved.getByText("검토 판단과 이력",{exact:true}).click();
  await expect(saved.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
  await saved.getByLabel("판단 사유",{exact:true}).fill("이 버전의 근거를 추가 검토");
  await saved.getByRole("button",{name:"판단 저장",exact:true}).click();
  await expect(saved.getByText("1개 판단 기록",{exact:true})).toBeVisible();
  await saved.getByLabel("검토 판단",{exact:true}).selectOption("accepted");
  await saved.getByLabel("판단 사유",{exact:true}).fill("당시 근거를 후속 검토에 채택");
  await saved.getByRole("button",{name:"판단 저장",exact:true}).click();
  await expect(saved.getByText("2개 판단 기록",{exact:true})).toBeVisible();
  const decisions=await(await page.request.get(`${root}/${original.version_id}/decisions`)).json();
  expect(decisions.items.map((d:{state:string})=>d.state)).toEqual(["accepted","held"]);
  // Change only this disposable fixture's topic label; generated content changes
  // while its theme-based candidate IDs remain the same.
  const change=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.services import topic_profiles
topic_profiles.update_topic_profile('${topicA}',{'title':'Changed local fixture topic'})
`],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
  expect(change.status,change.error?.message||change.stderr).toBe(0);
  await panel.getByRole("button",{name:"현재 추천 새로고침"}).click();
  await expect(panel.getByText("현재 추천을 새로고침했습니다.")).toBeVisible();
  const changed=await(await page.request.get(`/api/topics/${topicA}/recommendations`)).json();
  const next=changed.items.find((c:{id:string;type:string})=>c.id===item.id&&c.type===item.type);
  expect(next.versionId).not.toBe(item.versionId);
  await panel.locator("summary").filter({hasText:next.title}).first().click();
  await panel.getByRole("button",{name:"이 버전 저장"}).first().click();
  await expect(panel.getByText("2개 저장 버전",{exact:true})).toBeVisible();
  const versions=(await(await page.request.get(root)).json()).items;
  expect(versions[1]).toEqual(original);
  expect((await(await page.request.get(`${root}/${next.versionId}/decisions`)).json()).total).toBe(0);
  await page.goto("about:blank");
  return {versions,decisions,firstId:original.version_id};
 },{timeout:45000});
 await test.step("restart owned backend",()=>backend.restart(),{timeout:40000});
 await test.step("reopen immutable output and decisions",async()=>{
  await page.goto(`/app/topics/${topicA}/insights`);
  await expect(panel.getByText("2개 저장 버전",{exact:true})).toBeVisible();
  expect((await(await page.request.get(root)).json()).items).toEqual(versions);
  expect((await(await page.request.get(`${root}/${firstId}/decisions`)).json()).items).toEqual(decisions.items);
  const saved=panel.getByRole("article").filter({hasText:firstId});
  await saved.locator("summary").first().click();
  await expect(saved.getByText(versions[1].content.display.evidenceRefs[0].quoted_or_summarized_basis,{exact:true})).toBeVisible();
  await saved.locator("summary").first().click();
  await saved.getByText("검토 판단과 이력",{exact:true}).click();
  await expect(saved.getByText("당시 근거를 후속 검토에 채택",{exact:true})).toBeVisible();
  await saved.scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"recommendation-versions-wide.png")});
  await page.setViewportSize({width:390,height:844});
  await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
  await saved.getByLabel("판단 사유",{exact:true}).scrollIntoViewIfNeeded();
  await expect(saved.getByRole("button",{name:"판단 저장",exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"recommendation-versions-narrow.png")});
 },{timeout:45000});
 await assertHygiene(ledger);
 expect(ledger.requests.filter(r=>/start-runs|\/actions|\/retrieval|\/schedules.*run|\/notifications.*send/.test(r.pathname))).toEqual([]);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{immutableVersions:true,decisionsPreserved:true,providerCalls:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});

test("saved recommendation history remains usable during a current-output HTTP failure",async({page,context,backend},info)=>{
 const root=`/api/topics/${topicA}/recommendation-versions`;
 const currentUrl=`http://127.0.0.1:8002/api/topics/${topicA}/recommendations`;
 const current=await(await page.request.get(currentUrl)).json();
 const item=current.items[0];
 const capture=await page.request.post(root,{data:{candidate_type:item.type,candidate_id:item.id,expected_version_id:item.versionId}});
 expect(capture.status()).toBe(201);
 const ledger=await installLedger(context,page);
 // Explicit HTTP fault injection only for current output; history is the real backend.
 await page.route(currentUrl,route=>route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({detail:"recommendation_repository_unavailable"})}));
 await page.goto(`/app/topics/${topicA}/insights`);
 const panel=page.getByRole("region",{name:"추천 버전과 검토 이력"});
 await expect(panel.getByText("추천 자료를 사용할 수 없습니다.")).toBeVisible();
 await expect(panel.getByText("1개 저장 버전",{exact:true})).toBeVisible();
 const saved=panel.getByRole("article");
 await saved.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(saved.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
 await saved.getByLabel("판단 사유",{exact:true}).fill("현재 출력 장애 중에도 저장된 근거 검토");
 await saved.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(saved.getByText("1개 판단 기록",{exact:true})).toBeVisible();
 await page.unroute(currentUrl);
 await panel.getByRole("button",{name:"현재 추천 새로고침"}).click();
 await expect(panel.getByText("현재 추천을 새로고침했습니다.")).toBeVisible();
 await expect(panel.getByText("추천 자료를 사용할 수 없습니다.")).toHaveCount(0);
 expect(ledger.requestTimings.filter(r=>r.status===503).map(r=>r.pathname)).toEqual([`/api/topics/${topicA}/recommendations`]);
 expect(ledger.consoleErrors).toHaveLength(1);
 expect(ledger.consoleErrors[0]).toMatch(/503/);
 expect(ledger.pageErrors).toEqual([]);
 expect(ledger.offOriginAttempts).toEqual([]);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{injectedCurrentHttp503:1,persistedHistoryUsable:true,retryRecovered:true,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});
