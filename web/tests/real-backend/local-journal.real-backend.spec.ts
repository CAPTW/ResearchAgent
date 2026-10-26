import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("human journal drafts keep frozen citations and revisions after source changes and restart",async({page,backend,context},info)=>{
 test.setTimeout(90000);
 const ledger=await installLedger(context,page);
 const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 await page.goto(`/app/topics/${topicA}/papers`);
 const rows=page.getByRole("table").locator("tbody input[type=checkbox]");
 await expect(rows.first()).toBeVisible();
 for(let index=0;index<3;index++)await rows.nth(index).check();
 await page.getByRole("button",{name:"선택 보고서 입력 고정"}).click();
 const panel=page.getByRole("region",{name:"선택 문헌 근거 초안"});
 await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
 const id=new URL(page.url()).searchParams.get("synthesis")!;
 const root=`/api/topics/${topicA}/local-synthesis/${id}`;
 for(let step=1;step<=3;step++){
  await panel.getByRole("button",{name:"다음 한 단계 실행"}).click();
  await expect(panel.getByText(`${step===3?"근거 초안 준비됨":"수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
 }
 const original=await(await page.request.get(root)).json();
 const reportBefore=await(await page.request.get(root+"/report")).json();
 await panel.getByText("근거 연결 검토 초안",{exact:true}).click();
 const editor=panel.getByRole("region",{name:"근거 연결 초안 편집"});
 await expect(editor.getByLabel("검토 초안 제목")).toBeVisible();
 await editor.getByLabel("검토 초안 제목").fill("보존 문헌 비교 검토");
 await editor.getByLabel("문장 1 내용").fill("선택 문헌의 보존 초록을 함께 검토할 필요가 있다.");
 await expect(editor.getByRole("button",{name:"검토 초안 새 버전 저장"})).toBeDisabled();
 await editor.getByText("문장 1 인용 선택",{exact:true}).click();
 const abstracts=editor.getByRole("checkbox",{name:/· 초록$/});
 await abstracts.nth(0).check();await abstracts.nth(1).check();
 await editor.getByRole("button",{name:"검토 문장 추가"}).click();
 await editor.getByLabel("문장 2 분류").selectOption("question");
 await editor.getByLabel("문장 2 내용").fill("이 발췌의 원래 문맥은 무엇인가?");
 await editor.getByText("문장 2 인용 선택",{exact:true}).click();
 const second=editor.getByRole("group",{name:"문장 2",exact:true});
 await second.getByRole("checkbox",{name:/· 리뷰 발췌$/}).first().check();
 await editor.getByRole("button",{name:"검토 초안 새 버전 저장"}).click();
 await expect(panel.getByText("검토 초안 이력 · 1개 버전")).toBeVisible();
 const saved=panel.getByRole("article",{name:"보존된 근거 연결 초안"});
 await expect(saved.getByRole("heading",{name:"보존 문헌 비교 검토",exact:true})).toBeVisible();
 const first=(await(await page.request.get(root+"/journal")).json()).latest_version;
 expect(first.entries).toHaveLength(2);
 expect(first.entries[0].citation_ids).toHaveLength(2);
 expect(first.citations.some((c:{kind:string})=>c.kind==="review_excerpt")).toBe(true);
 expect(first.validation.scientific_assessment).toBe("NOT_ASSESSED");
 await editor.getByLabel("문장 1 내용").fill("초록만으로 일반화하지 않고 원문 문맥을 추가 검토한다.");
 await editor.getByRole("button",{name:"검토 초안 새 버전 저장"}).click();
 await expect(panel.getByText("검토 초안 이력 · 2개 버전")).toBeVisible();
 const latest=(await(await page.request.get(root+"/journal")).json()).latest_version;
 expect(latest.previous_id).toBe(first.version_id);
 await panel.getByRole("button",{name:new RegExp(first.version_id)}).click();
 await expect(saved.getByRole("region",{name:"보존 문장 1"}).getByText("선택 문헌의 보존 초록을 함께 검토할 필요가 있다.",{exact:true})).toBeVisible();
 await expect(saved.getByText("초록만으로 일반화하지 않고 원문 문맥을 추가 검토한다.",{exact:true})).toHaveCount(0);
 for(const [label,filename] of [["검토 초안 Markdown 내려받기","journal-first.md"],["검토 초안 근거 JSON 내려받기","journal-first.json"]]){
  const [download]=await Promise.all([page.waitForEvent("download"),saved.getByRole("button",{name:label}).click()]);
  const file=path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,filename);
  await download.saveAs(file);expect(await download.failure()).toBeNull();
  const content=fs.readFileSync(file,"utf8");
  if(filename.endsWith(".json")){const bundle=JSON.parse(content);expect(bundle.version).toEqual(first);expect(bundle.frozen_input).toEqual(original.inputs);}
  else expect(content).toBe(first.markdown);
 }
 const mutate=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.repositories import runs
from app.repositories.files import read_jsonl,write_jsonl
for run in runs.STORAGE_ROOT.iterdir():
 p=run/'triage'/'triage.jsonl'
 if p.is_file():
  rows=read_jsonl(p)
  for row in rows:row['abstract']='Current abstract changed after journal'
  write_jsonl(p,rows)
`],{cwd:backend.sourceRoot,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:15000});
 expect(mutate.status,mutate.stderr).toBe(0);
 const url=page.url();await page.goto("about:blank");await backend.restart();await page.goto(url);
 await panel.getByText("근거 연결 검토 초안",{exact:true}).click();
 await expect(panel.getByText("검토 초안 이력 · 2개 버전")).toBeVisible();
 expect(await(await page.request.get(root+"/journal/"+first.version_id)).json()).toEqual(first);
 expect(await(await page.request.get(root+"/journal/"+latest.version_id)).json()).toEqual(latest);
 expect(await(await page.request.get(root+"/report")).json()).toEqual(reportBefore);
 await expect(editor.getByLabel("문장 1 내용")).toHaveValue("초록만으로 일반화하지 않고 원문 문맥을 추가 검토한다.");
 await panel.getByRole("button",{name:new RegExp(first.version_id)}).click();
 await expect(saved).not.toContainText("Current abstract changed after journal");
 await saved.getByText(/저장된 초록 ·/).first().click();
 await saved.scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"journal-wide.png")});
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await saved.getByRole("heading",{name:"보존 문헌 비교 검토",exact:true}).scrollIntoViewIfNeeded();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"journal-narrow.png")});
 await assertHygiene(ledger);
 expect(ledger.mutationRequests.every(value=>value.includes("/local-synthesis"))).toBe(true);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{humanAuthored:true,exactCitations:true,twoRevisions:true,oldVersionDownload:true,restartPreserved:true,unchangedReport:true,automaticRuns:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
});
