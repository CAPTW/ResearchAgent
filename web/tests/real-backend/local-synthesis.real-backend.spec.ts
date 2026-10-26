import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("selected frozen inputs resume after restart without reading changed sources",async({page,backend,context},info)=>{
 test.setTimeout(100000);
 const ledger=await installLedger(context,page);
 const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 await page.goto(`/app/topics/${topicA}/papers`);
 // Use visible version rows; no synthetic selection injected into the UI.
 const rows=page.getByRole("table").locator("tbody input[type=checkbox]");
 await expect(rows.first()).toBeVisible();
 for(let index=0;index<6;index++)await rows.nth(index).check();
 await page.getByRole("button",{name:"선택 보고서 입력 고정"}).click();
 const panel=page.getByRole("region",{name:"선택 문헌 근거 초안"});
 await expect(panel.getByText("수동 진행 가능 · 0 / 4 단계")).toBeVisible();
 const id=new URL(page.url()).searchParams.get("synthesis")!;
 expect(id).toMatch(/^syn_/);
 const root=`/api/topics/${topicA}/local-synthesis/${id}`;
 const original=await(await page.request.get(root)).json();
 expect(original.inputs.papers).toHaveLength(6);
 await panel.getByRole("button",{name:"다음 한 단계 실행"}).click();
 await expect(panel.getByText("수동 진행 가능 · 1 / 4 단계")).toBeVisible();
 await panel.getByRole("button",{name:"진행 일시 중지"}).click();
 await expect(panel.getByText("일시 중지 · 1 / 4 단계")).toBeVisible();
 const paused=await(await page.request.get(root)).json();
 // Mutate only the disposable fixture's current source after capture.
 const change=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.repositories import runs
from app.repositories.files import read_jsonl,write_jsonl
for run in runs.STORAGE_ROOT.iterdir():
 p=run/'triage'/'triage.jsonl'
 if p.is_file():
  items=read_jsonl(p)
  for item in items:item['abstract']='Changed after frozen capture'
  write_jsonl(p,items)
`],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
 expect(change.status,change.error?.message||change.stderr).toBe(0);
 const url=page.url();
 await page.goto("about:blank");
 await backend.restart();
 await page.goto(url);
 await expect(panel.getByText("일시 중지 · 1 / 4 단계")).toBeVisible();
 expect(await(await page.request.get(root)).json()).toEqual(paused);
 await panel.getByRole("button",{name:"수동 진행 재개"}).click();
 await expect(panel.getByText("수동 진행 가능 · 1 / 4 단계")).toBeVisible();
 for(let step=2;step<=4;step++){
  await panel.getByRole("button",{name:"다음 한 단계 실행"}).click();
  await expect(panel.getByText(`${step===4?"근거 초안 준비됨":"수동 진행 가능"} · ${step} / 4 단계`)).toBeVisible();
 }
 const final=await(await page.request.get(root)).json();
 expect(final.inputs).toEqual(original.inputs);
 expect(final.steps[0]).toEqual(paused.steps[0]);
 expect(final.candidate.coverage.contradiction_count).toBeNull();
 await expect(panel.getByLabel("고정 입력으로 만든 근거 초안")).not.toContainText("Changed after frozen capture");
 const replay=await page.request.post(root+"/step",{data:{expected_step:1}});
 expect(await replay.json()).toEqual({job:final,advanced:false});
 const reportPanel=panel.getByRole("region",{name:"로컬 근거 보고서 채택과 내보내기"});
 await reportPanel.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(reportPanel.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
 await reportPanel.getByLabel("검토 판단",{exact:true}).selectOption("accepted");
 await reportPanel.getByLabel("판단 사유",{exact:true}).fill("보존된 입력과 발췌를 확인하여 채택");
 await reportPanel.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(reportPanel.getByText("1개 판단 기록",{exact:true})).toBeVisible();
 await expect(reportPanel.getByText("현재 판단: 근거 보고서로 채택",{exact:true})).toBeVisible();
 const accepted=await(await page.request.get(root+"/report")).json();
 for(const [label,filename] of [["Markdown 내보내기","accepted-evidence.md"],["근거 JSON 내보내기","accepted-evidence.json"]]){
  const [download]=await Promise.all([page.waitForEvent("download"),reportPanel.getByRole("button",{name:label,exact:true}).click()]);
  const file=path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,filename);
  await download.saveAs(file);
  expect(await download.failure()).toBeNull();
  const content=fs.readFileSync(file,"utf8");
  if(filename.endsWith(".json")){
   const bundle=JSON.parse(content);
   expect(bundle.frozen_input).toEqual(original.inputs);
   expect(bundle.version).toEqual(accepted.version);
   expect(bundle.decisions[0].decision_id).toBe(accepted.latest_decision.decision_id);
  }else expect(content).toContain(accepted.version.content);
 }
 await reportPanel.getByLabel("검토 판단",{exact:true}).selectOption("held");
 await reportPanel.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(reportPanel.getByText("현재 판단: 보류",{exact:true})).toBeVisible();
 await expect(reportPanel.getByRole("button",{name:"Markdown 내보내기",exact:true})).toBeDisabled();
 await expect(reportPanel.getByRole("button",{name:"Markdown 내보내기",exact:true})).toBeVisible();
 await reportPanel.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-report-held.png")});
 const held=await(await page.request.get(root+"/report")).json();
 expect(held.version).toEqual(accepted.version);
 const denied=await page.request.get(root+"/report/export",{params:{format:"json",version_id:accepted.version.version_id,decision_id:accepted.latest_decision.decision_id}});
 expect(denied.status()).toBe(409);
 await page.goto("about:blank");
 await backend.restart();
 await page.goto(url);
 await expect(reportPanel.getByText("현재 판단: 보류",{exact:true})).toBeVisible();
 expect(await(await page.request.get(root+"/report")).json()).toEqual(held);
 await expect(panel.getByRole("button",{name:/보류 · 보존 버전 있음/})).toHaveCount(1);
 await page.reload();
 await expect(panel.getByText("검토할 근거 초안")).toBeVisible();
 await panel.getByText("고정 입력 6편과 근거",{exact:true}).click();
 await panel.getByRole("article",{name:"고정 입력과 진행 상태"}).scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-synthesis-wide.png")});
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await panel.getByLabel("고정 입력으로 만든 근거 초안").scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-synthesis-narrow.png")});
 // Capture the changed current fixture as a second report through the user flow.
 await page.setViewportSize({width:1280,height:900});
 await page.goto(`/app/topics/${topicA}/papers`);
 await expect(rows.first()).toBeVisible();
 for(let index=0;index<5;index++)await rows.nth(index).check();
 await page.getByRole("button",{name:"선택 보고서 입력 고정"}).click();
 await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
 const secondId=new URL(page.url()).searchParams.get("synthesis")!;
 expect(secondId).not.toBe(id);
 for(let step=1;step<=3;step++){
  await panel.getByRole("button",{name:"다음 한 단계 실행"}).click();
  await expect(panel.getByText(`${step===3?"근거 초안 준비됨":"수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
 }
 await reportPanel.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(reportPanel.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
 await reportPanel.getByLabel("검토 판단",{exact:true}).selectOption("accepted");
 await reportPanel.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(reportPanel.getByText("현재 판단: 근거 보고서로 채택",{exact:true})).toBeVisible();
 await reportPanel.getByText("보존 보고서 읽기",{exact:true}).click();
 const reader=reportPanel.getByRole("article",{name:"로컬 근거 보고서 읽기"});
 await expect(reader.getByRole("heading",{name:"범위와 근거",exact:true})).toBeVisible();
 await expect(reader).toContainText("Changed after frozen capture");
 await expect(reader.locator("img,iframe,script,a")).toHaveCount(0);
 await reader.scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-report-reader.png")});
 await reportPanel.getByRole("button",{name:"다른 보존 보고서와 비교"}).click();
 const compare=reportPanel.getByRole("region",{name:"보존 보고서 비교"});
 await expect(compare.getByText("2개 보존 보고서 · 1페이지")).toBeVisible();
 await expect(compare.getByRole("table")).toHaveCount(0);
 await compare.getByRole("button",{name:/비교 기준으로 선택 · 6편/}).click();
 await expect(compare.getByText("논문 버전: 추가 0 · 제외 1 · 기록 변경 5 · 같음 0")).toBeVisible();
 await expect(compare.getByText("확인 시 판단: 보류",{exact:true})).toBeVisible();
 const comparisonUrl=`/api/topics/${topicA}/local-synthesis/${secondId}/report/comparison?from_job_id=${id}`;
 const comparison=await(await page.request.get(comparisonUrl)).json();
 expect(comparison.before.content_sha256).toBe(held.version.content_sha256);
 const changedPaper=compare.locator("details").filter({has:page.locator("summary",{hasText:/^기록 변경 ·/})}).first();
 await changedPaper.locator(":scope > summary").click();
 await changedPaper.getByText("초록",{exact:true}).click();
 await expect(changedPaper.getByText("Changed after frozen capture",{exact:true})).toBeVisible();
 await compare.scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-report-comparison.png")});
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await changedPaper.scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"local-report-comparison-narrow.png")});
 const secondUrl=page.url();
 await page.goto("about:blank");
 await backend.restart();
 await page.goto(secondUrl);
 expect(await(await page.request.get(comparisonUrl)).json()).toEqual(comparison);
 expect(await(await page.request.get(root+"/report")).json()).toEqual(held);
 await reportPanel.getByRole("button",{name:"다른 보존 보고서와 비교"}).click();
 await compare.getByRole("button",{name:/비교 기준으로 선택 · 6편/}).click();
 await expect(compare.getByText("논문 버전: 추가 0 · 제외 1 · 기록 변경 5 · 같음 0")).toBeVisible();
 await assertHygiene(ledger);
 expect(ledger.mutationRequests.every(value=>value.includes("/local-synthesis"))).toBe(true);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{selectedPapers:6,frozenSources:true,restartPreserved:true,checkpointReplay:true,explicitAcceptance:true,markdownDownload:true,evidenceBundleDownload:true,heldExportRefused:true,readableReport:true,twoReportComparison:true,comparisonAfterRestart:true,contradictionsUnassessed:true,automaticRuns:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});
