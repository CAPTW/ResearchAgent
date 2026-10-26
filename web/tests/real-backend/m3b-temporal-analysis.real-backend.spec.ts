import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test as base, expect, topicA, installLedger, assertHygiene, type BrowserLedger } from "./m2-paper-bank.real-backend.helpers";

const test=base.extend<{analysisLedger:BrowserLedger}>({analysisLedger:[async({backend,context,page},use,info)=>{
 const ledger=await installLedger(context,page);const runs=fs.readdirSync(path.join(backend.dataRoot,"runs")).sort();
 await use(ledger);await assertHygiene(ledger);
 expect(ledger.requests.filter(r=>/start-runs|\/actions|\/retrieval|\/schedules.*run|\/notifications.*send/.test(r.pathname))).toEqual([]);
 expect(fs.readdirSync(path.join(backend.dataRoot,"runs")).sort()).toEqual(runs);
 backend.writeJourneyResult({journey:info.title,migrationIds:[],assertions:{providerCalls:0,retrievalCalls:0,automaticRuns:0,schedulerCalls:0,notificationCalls:0,passed:info.status==="passed"},timing:{},hygiene:{consoleErrors:ledger.consoleErrors.length,pageErrors:ledger.pageErrors.length,offOriginAttempts:ledger.offOriginAttempts.length}});
},{auto:true}]});

test("m3b-policy-create-and-first-snapshot-analysis",async({page})=>{
 await page.goto(`/app/topics/${topicA}/insights`);
 const p=page.getByRole("region",{name:"스냅샷 기반 분석"});
 await expect(p.getByText("활성 정책 없음")).toBeVisible();
 await p.getByRole("button",{name:"권장 기본 정책 만들기"}).click();
 await p.getByRole("button",{name:"정책 활성화"}).click();
 await page.getByRole("button",{name:"현재 상태 스냅샷 만들기"}).click();
 await expect(page.getByText(/스냅샷이 하나 있습니다/)).toBeVisible();
 // Capturing in the same workspace must immediately update the analysis target.
 await expect(p.getByRole("button",{name:"선택 스냅샷 분석"})).toBeEnabled();
 await p.getByRole("button",{name:"선택 스냅샷 분석"}).click();
 await expect(p.getByText("분석 기록을 저장했습니다.")).toBeVisible();
 await expect(p.getByRole("table",{name:"정확한 측정값"})).toContainText("AVAILABLE");
 await p.getByLabel("측정 지표").selectOption("delta.paper_added_count");
 await expect(p.getByRole("table")).toContainText("INSUFFICIENT_BASELINE");
 await expect(p.getByText("INSUFFICIENT_DATA",{exact:true})).toHaveCount(6);
 const policies=await(await page.request.get(`/api/topics/${topicA}/analysis/policies`)).json();
 expect(policies.active_policy_id).toBe(policies.items[0].policy_id);
 await page.reload();await expect(p.getByRole("table")).toContainText("AVAILABLE");
 await p.getByRole("button",{name:"선택 스냅샷 분석"}).click();
 await expect(p.getByText("기존 분석 기록을 반환했습니다.")).toBeVisible();
 await p.getByText("새 불변 정책 버전 설정",{exact:true}).click();
 await p.getByLabel("최소 논문 수",{exact:true}).fill("100");
 await p.getByRole("button",{name:"새 정책 버전 만들기"}).click();
 await p.getByRole("button",{name:"정책 활성화",exact:true}).click();
 await p.getByRole("button",{name:"선택 스냅샷 분석"}).click();
 await expect(p.getByText("분석 기록을 저장했습니다.")).toBeVisible();
 await expect(p.getByRole("img",{name:/스냅샷 측정점/})).toHaveCount(2);
 const history=await(await page.request.get(`/api/topics/${topicA}/analysis/series?metric_id=corpus.paper_count`)).json();
 expect(new Set(history.items.map((point:{policy_id:string})=>point.policy_id)).size).toBe(2);
 expect(history.items.some((point:{policy_id:string})=>point.policy_id===policies.active_policy_id)).toBe(true);
});

const changedHistoryTest=test.extend<{preparedHistory:void}>({preparedHistory:[async({backend},use)=>{
 // The existing seed already provides authoritative paper/review identities.
 const setup=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.repositories import temporal_memory as m, temporal_sources as s, temporal_analysis as a
from app.services import paper_bank
from app.repositories import paper_reviews
t='${topicA}'
p,_=a.create_policy(t,{})
a.activate(t,p.policy_id)
x,_=m.capture(t,lambda c:s.project(c,t))
a.evaluate(t,x.snapshot_id)
from tests.helpers.m2_paper_bank_fixture import _paper, _review, RUN_A_2
from app.repositories.runs import build_run_paths
from app.repositories.files import write_jsonl
import json
path=build_run_paths(RUN_A_2).triage_dir/'triage.jsonl'
rows=[json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
rows.append(_paper('2609.12345v1','M3B added local fixture',provider='arxiv',published='2026-09-18',authors=['Local fixture'],abstract='Synthetic fixture',entry_url='https://arxiv.org/abs/2609.12345v1',open_access=True))
write_jsonl(path,rows)
_review(run_id=RUN_A_2,paper_id='2609.12345v1',title='M3B added local fixture',full_text_status='reviewed',evidence_spans=[{'evidence_span_id':'m3b-new-evidence','content_sha256':'a'*64,'locator':{'page':1},'text':'Synthetic fixture evidence.'}])
from app.repositories import paper_collections
bank=paper_bank.list_topic_papers(t,page_size=100)
v=next(item for item in bank['items'] if item['sourceIdentity']['nativeId']!='2609.12345v1' and 'saved-papers' not in item['collectionIds'])
paper_collections.add_member(t,'saved-papers',canonical_paper_id=v['canonicalPaperId'],paper_version_id=v['paperVersionId'])
x,_=m.capture(t,lambda c:s.project(c,t))
a.evaluate(t,x.snapshot_id)
`],{cwd:process.env.XRML_M2_SOURCE_ROOT,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:60000});
 expect(setup.status,setup.error?.message||setup.stderr).toBe(0);
 await use();
},{auto:true,timeout:90000}]});

changedHistoryTest("m3b-second-snapshot-series-and-factual-insights",async({page,backend})=>{
 // Two browser sessions plus an owned cold restart, each with its own bound.
 changedHistoryTest.setTimeout(130000);
 const p=page.getByRole("region",{name:"스냅샷 기반 분석"});
 const {counts,data,acceptedId,decisions}=await changedHistoryTest.step("inspect history and open evidence",async()=>{
 await page.goto(`/app/topics/${topicA}/insights`);
 await expect(p.getByRole("table").getByRole("row")).toHaveCount(3);
 const counts=await(await page.request.get(`/api/topics/${topicA}/analysis/series?metric_id=corpus.paper_count`)).json();
 expect(counts.items).toHaveLength(2);expect(counts.items[1].count_value-counts.items[0].count_value).toBe(1);
 await expect(p.getByRole("table").getByRole("row").nth(1).getByRole("cell").nth(1)).toHaveText(String(counts.items[0].count_value));
 await expect(p.getByRole("table").getByRole("row").nth(2).getByRole("cell").nth(1)).toHaveText(String(counts.items[1].count_value));
 const coverage=await(await page.request.get(`/api/topics/${topicA}/analysis/series?metric_id=coverage.reviewed_paper_ratio`)).json();
 await p.getByLabel("측정 지표").selectOption("coverage.reviewed_paper_ratio");
 await expect(p.getByRole("table").getByRole("row").nth(2).getByRole("cell").nth(1)).toContainText(`${coverage.items[1].numerator}/${coverage.items[1].denominator}`);
 await p.getByLabel("측정 지표").selectOption("delta.collection_membership_changed_count");
 await expect(p.getByRole("table").getByRole("row").nth(2).getByRole("cell").nth(1)).toHaveText("1");
 await expect(p.getByText("M3B_METRIC_FORMULA_REGISTRY_V1",{exact:false}).first()).toBeVisible();
 const data=await(await page.request.get(`/api/topics/${topicA}/analysis/insights`)).json();
 expect(data.items.length).toBe(12);expect(data.items.every((i:{metric_point_ids:string[]})=>i.metric_point_ids.length>0)).toBe(true);
 const accepted=data.items.find((i:{insight_kind:string;state:string})=>i.insight_kind==="CORPUS_SIZE_CHANGE"&&i.state==="ACCEPTED");
 expect(accepted.absolute_delta).toBe(1);
 await page.reload();await expect(p.getByRole("table").getByRole("row")).toHaveCount(3);
 await p.getByRole("table").scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"m3b-series-wide.png"),fullPage:true});
 await expect(p.getByText(accepted.rendered_factual_text,{exact:true})).toBeVisible();
 const card=p.getByRole("article").filter({hasText:accepted.rendered_factual_text});
 await card.getByText("분석 근거와 버전",{exact:true}).click();
 await expect(card.getByText(/M3B_FACTUAL_INSIGHT_TEMPLATE_V1/)).toBeVisible();
 await card.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(card.getByText("아직 사용자 판단이 없습니다.")).toBeVisible();
 await card.getByLabel("판단 사유",{exact:true}).fill("본문을 추가로 읽은 뒤 판단");
 await card.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(card.getByText("1개 판단 기록",{exact:true})).toBeVisible();
 await card.getByLabel("검토 판단",{exact:true}).selectOption("accepted");
 await card.getByLabel("판단 사유",{exact:true}).fill("이 근거를 후속 검토에 사용");
 await card.getByRole("button",{name:"판단 저장",exact:true}).click();
 await expect(card.getByText("2개 판단 기록",{exact:true})).toBeVisible();
 const decisions=await(await page.request.get(`/api/topics/${topicA}/analysis/insights/${accepted.insight_record_id}/decisions`)).json();
 expect(decisions.items.map((d:{state:string})=>d.state)).toEqual(["accepted","held"]);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"m3b-provenance-wide.png"),fullPage:true});
 const evidence=card.getByRole("link",{name:"근거 m3b-new-evidence"});
 await evidence.click();
 await expect(page).toHaveURL(/evidence=m3b-new-evidence/);
 await expect(page.getByText("Synthetic fixture evidence.",{exact:true})).toBeVisible();
 // Close the UI before stopping its server; do not reset in-flight page requests.
 await page.goto("about:blank");
 return {counts,data,acceptedId:accepted.insight_record_id,decisions};
 },{timeout:45000});
 // Reopen the same immutable history after an actual sidecar process restart.
 await changedHistoryTest.step("restart owned sidecar",()=>backend.restart(),{timeout:40000});
 await changedHistoryTest.step("reopen and select historical records",async()=>{
 await page.goto(`/app/topics/${topicA}/insights`);
 await expect(p.getByRole("table").getByRole("row")).toHaveCount(3);
 const reopened=await(await page.request.get(`/api/topics/${topicA}/analysis/insights`)).json();
 expect(reopened.items).toEqual(data.items);
 const reopenedDecisions=await(await page.request.get(`/api/topics/${topicA}/analysis/insights/${acceptedId}/decisions`)).json();
 expect(reopenedDecisions.items).toEqual(decisions.items);
 // Locate by the exact stored factual sentence, independent of display language.
 const fact=data.items.find((i:{insight_record_id:string})=>i.insight_record_id===acceptedId);
 const decisionCard=p.getByRole("article").filter({hasText:fact.rendered_factual_text});
 await decisionCard.getByText("검토 판단과 이력",{exact:true}).click();
 await expect(decisionCard.getByText("이 근거를 후속 검토에 사용",{exact:true})).toBeVisible();
 await decisionCard.scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"decisions-reopened-wide.png"),fullPage:false});
 await page.setViewportSize({width:390,height:844});
 await expect.poll(async()=>{const b=await page.locator("#v2-sidebar").boundingBox();return b?b.x+b.width:0;}).toBeLessThanOrEqual(0);
 await decisionCard.getByLabel("검토 판단",{exact:true}).scrollIntoViewIfNeeded();
 await expect(decisionCard.getByRole("button",{name:"판단 저장",exact:true})).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"decisions-reopened-narrow.png"),fullPage:false});
 await page.setViewportSize({width:1536,height:1024});
 await page.getByRole("combobox",{name:"스냅샷 이력",exact:true}).selectOption(counts.items[0].source_snapshot_id);
 await expect(page.getByText("선택한 첫 스냅샷에는 이전 비교 기준이 없습니다.")).toBeVisible();
 await p.getByLabel("선택 스냅샷 기록만 보기",{exact:true}).check();
 await expect(p.getByRole("table").getByRole("row")).toHaveCount(2);
 await expect(p.getByRole("article")).toHaveCount(6);
 await p.getByRole("button",{name:"선택 스냅샷 분석"}).click();
 await expect(p.getByText("기존 분석 기록을 반환했습니다.")).toBeVisible();
 },{timeout:45000});
});

const stateHistoryTest=test.extend<{preparedHistory:void}>({preparedHistory:[async({backend},use)=>{
 const seed=spawnSync(process.env.XRML_M2_PYTHON_EXE!,["-c",`from app.repositories import temporal_memory as m, temporal_analysis as a
from app.domain.temporal_memory import Member
t='${topicA}'
p,_=a.create_policy(t,{})
a.activate(t,p.policy_id)
for members in [[],[Member('paper-test','version-test')],[Member('paper-test','version-test',collection_ids=('saved',))]]:
 x,_=m.capture(t,lambda c:members)
 a.evaluate(t,x.snapshot_id)
`],{cwd:process.env.XRML_M2_SOURCE_ROOT,env:{...process.env,XRML_DATA_DIR:backend.dataRoot},encoding:"utf8",windowsHide:true,timeout:60000});
 expect(seed.status,seed.error?.message||seed.stderr).toBe(0);
 await use();
},{auto:true,timeout:90000}]});

stateHistoryTest("m3b-insufficient-and-unavailable-truthfulness",async({page})=>{
 await page.goto(`/app/topics/${topicA}/insights`);const p=page.getByRole("region",{name:"스냅샷 기반 분석"});
 await p.getByLabel("측정 지표").selectOption("coverage.reviewed_paper_ratio");
 await expect(p.getByRole("table")).toContainText("INSUFFICIENT_DENOMINATOR");
 await expect(p.getByRole("table")).toContainText("UNAVAILABLE");
 await expect(p.getByRole("table")).not.toContainText("0%");
 await expect(p.getByText("BELOW_POLICY_THRESHOLD",{exact:true}).first()).toBeVisible();
 await expect(p.getByRole("article").getByText("UNAVAILABLE",{exact:true}).first()).toBeVisible();
 await expect(page.getByText("API 평균 영향 점수",{exact:true})).toHaveCount(0);
 await expect(page.getByText("수집 추세 변화",{exact:true})).toHaveCount(0);
 await expect(p.getByRole("button",{name:"선택 스냅샷 분석"})).toBeEnabled();
 await page.setViewportSize({width:390,height:844});
 await expect(p.getByRole("heading",{name:"스냅샷 기반 분석"})).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!,"m3b-states-narrow.png"),fullPage:true});
});
