import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("frozen citations and human report acceptance survive changed sources and restart",async({page,backend,context},info)=>{
 test.setTimeout(130000);
 const run="m2-paper-bank-topic-a-run-1";
 const python=(script:string)=>{
  const result=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",script],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
  expect(result.status,result.error?.message||result.stderr).toBe(0);
 };
 python(`from app.services import report_versions as v, reporting as r
from app.repositories.files import write_json,write_text,read_json
from app.repositories.runs import build_run_paths
run='${run}'
p=build_run_paths(run)
outline=r.load_outline(run)
outline['sections'][0]['claims']=[dict(claim_id='claim-local',claim_text='Local saved claim',citations=[dict(evidence_id='evidence-local',source_id='source-local',citation_text='Saved source',snippet='Original frozen excerpt')])]
write_json(p.reports_dir/'outline.json',outline)
content=r.build_preview(run,persist=False)['content']
source=p.reports_dir/'frozen-journal.md'
write_text(source,content)
record=read_json(p.run_json_path)
record['artifacts']['report_markdown']=str(source)
write_json(p.run_json_path,record)
v.record_completed_report_version(run,source='synthetic_citation_fixture',source_artifact_path=str(source),content=content)
outline['sections'][0]['claims'][0]['citations'][0]['snippet']='Changed current excerpt'
write_json(p.reports_dir/'outline.json',outline)
`);
 const ledger=await installLedger(context,page);
 const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 await page.goto(`/app/topics/${topicA}/reports`);
 await page.getByRole("button",{name:/저장 버전 열기/}).first().click();
 const panel=page.getByRole("region",{name:"보고서 저장 버전"});
 await panel.getByText("이 버전에 보존된 인용",{exact:true}).click();
 await expect(panel.getByRole("article",{name:"보존된 인용 상세"})).toContainText("Original frozen excerpt");
 await expect(panel.getByText("Changed current excerpt",{exact:true})).toHaveCount(0);
 await panel.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(panel.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
 await panel.getByLabel("검토 판단",{exact:true}).selectOption("accepted");
 await panel.getByLabel("판단 사유",{exact:true}).fill("저장된 근거와 보고서 버전을 검토하여 채택");
 await panel.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(panel.getByText("1개 판단 기록",{exact:true})).toBeVisible();
 const version=(await(await page.request.get(`/api/reports/${run}/versions`)).json()).items[0];
 const root=`/runs/${run}/report/versions/${version.version_id}/decisions`;
 const history=await(await page.request.get(root)).json();
 expect(history.items[0].state).toBe("accepted");
 expect(history.items[0].citation_status).toBe("VERIFIED");
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await panel.getByRole("article",{name:"보존된 인용 상세"}).scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"report-frozen-citation-narrow.png")});
 await page.goto("about:blank");
 await backend.restart();
 await page.goto(`/app/topics/${topicA}/reports`);
 await page.getByRole("button",{name:/저장 버전 열기/}).first().click();
 await panel.getByText("이 버전에 보존된 인용",{exact:true}).click();
 await expect(panel.getByRole("article",{name:"보존된 인용 상세"})).toContainText("Original frozen excerpt");
 expect(await(await page.request.get(root)).json()).toEqual(history);
 python(`from app.services import report_versions as v
from app.repositories.runs import build_run_paths
run='${run}'
record=v.list_report_versions(run)['items'][0]
p=build_run_paths(run).reports_dir/record['citations_snapshot']['filename']
assert p.resolve().is_relative_to(build_run_paths(run).reports_dir.resolve())
p.write_text('changed fixture citation',encoding='utf-8')
`);
 await panel.getByRole("button",{name:"보고서 버전 새로고침"}).click();
 await panel.getByText("이 버전에 보존된 인용",{exact:true}).click();
 await expect(panel.getByText("보존된 인용 파일이 없거나 읽을 수 없습니다.",{exact:true})).toBeVisible();
 await panel.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(panel.getByText("1개 판단 기록",{exact:true})).toBeVisible();
 await expect(panel.getByRole("option",{name:"이 보고서 버전 채택"})).toBeDisabled();
 await assertHygiene(ledger);
 expect(ledger.mutationRequests.every(value=>value.includes("/decisions"))).toBe(true);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{frozenCitations:true,explicitAcceptance:true,restartPreserved:true,damagedCitationRefused:true,automaticRuns:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});

test("stored report versions open, compare, and reject a changed snapshot",async({page,backend,context},info)=>{
 const seed=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from tests.helpers.m2_paper_bank_fixture import RUN_A_1
from app.repositories.runs import build_run_paths
from app.repositories.files import write_text,read_json,write_json
from app.services import report_versions
p=build_run_paths(RUN_A_1)
source=p.reports_dir/'local-journal.md'
run=read_json(p.run_json_path)
run['artifacts']['report_markdown']=str(source)
write_json(p.run_json_path,run)
for number in (1,2):
 content=f'# Local Journal\\n\\n## Evidence\\nSaved local observation {number}. [paper-1]\\n'
 write_text(source,content)
 report_versions.record_completed_report_version(RUN_A_1,source='synthetic_report_fixture',source_artifact_path=str(source),content=content)
`],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
 expect(seed.status,seed.error?.message||seed.stderr).toBe(0);
 const ledger=await installLedger(context,page);
 const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 await page.goto(`/app/topics/${topicA}/reports`);
 await page.getByRole("button",{name:/local journal 저장 버전 열기/i}).click();
 const panel=page.getByRole("region",{name:"보고서 저장 버전"});
 await expect(panel.getByLabel("저장된 Markdown 본문")).toContainText("Saved local observation 2.");
 const payload=await(await page.request.get('/api/reports/m2-paper-bank-topic-a-run-1/versions')).json();
 const first=payload.items[0].version_id,second=payload.items[1].version_id;
 await panel.getByLabel("비교 기준 버전",{exact:true}).selectOption(first);
 await expect(panel.getByText(/변경 1/)).toBeVisible();
 await panel.getByText("Evidence · 변경",{exact:true}).click();
 await expect(panel.getByText("Saved local observation 1. [paper-1]",{exact:true})).toBeVisible();
 await panel.getByLabel("읽을 보고서 버전",{exact:true}).selectOption(first);
 await expect(panel.getByLabel("저장된 Markdown 본문")).toContainText("Saved local observation 1.");
 await panel.getByText("저장 버전과 근거 출처",{exact:true}).click();
 await expect(panel.getByText("본문 확인: SHA-256 일치",{exact:true})).toBeVisible();
 await panel.scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"report-version-wide.png")});
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await panel.getByLabel("저장된 Markdown 본문").scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"report-version-narrow.png")});
 await page.reload();
 await page.getByRole("button",{name:/local journal 저장 버전 열기/i}).click();
 await panel.getByLabel("읽을 보고서 버전",{exact:true}).selectOption(first);
 await panel.getByLabel("비교 기준 버전",{exact:true}).selectOption(first);
 await expect(panel.getByLabel("저장된 Markdown 본문")).toContainText("Saved local observation 1.");
 // Replace only a known snapshot in the disposable fixture, never user data.
 const change=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.services import report_versions as r
from app.repositories.runs import build_run_paths
v=r.list_report_versions('m2-paper-bank-topic-a-run-1')['items'][0]
p=r._resolve_snapshot_path('m2-paper-bank-topic-a-run-1',v['snapshot_path'])
assert p.is_relative_to(build_run_paths('m2-paper-bank-topic-a-run-1').reports_dir.resolve())
p.write_text('Changed snapshot should never be displayed',encoding='utf-8')
`],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
 expect(change.status,change.error?.message||change.stderr).toBe(0);
 await panel.getByRole("button",{name:"보고서 버전 새로고침"}).click();
 await expect(panel.getByText(/원래 버전의 본문으로 표시할 수 없습니다/)).toBeVisible();
 await expect(panel.getByLabel("저장된 Markdown 본문")).toHaveCount(0);
 await panel.getByLabel("읽을 보고서 버전",{exact:true}).selectOption(second);
 await expect(panel.getByLabel("저장된 Markdown 본문")).toContainText("Saved local observation 2.");
 await expect(panel.getByText(/검증할 수 없어 비교하지 않았습니다/)).toBeVisible();
 await expect(panel.getByText(/변경 0/)).toHaveCount(0);
 await assertHygiene(ledger);
 expect(ledger.mutationRequests).toEqual([]);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{verifiedVersionRead:true,diff:true,corruptSnapshotRefused:true,automaticRuns:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});
