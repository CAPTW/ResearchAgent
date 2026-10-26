import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { TemporalAnalysisPanel } from "../views/TemporalAnalysisPanel";
import { TemporalResearchPanel } from "../views/TemporalResearchPanel";
import * as api from "../../lib/temporalAnalysis";
import * as memory from "../../lib/temporalMemory";
import { ApiError } from "../../lib/api";
import { AppearanceProvider } from "../AppearanceProvider";
import { APPEARANCE_STORAGE_KEY, DEFAULT_APPEARANCE, formatTimestamp } from "../appearance";

vi.mock("../../lib/temporalAnalysis", async original => ({ ...await original<typeof api>(), listPolicies:vi.fn(), createPolicy:vi.fn(), activatePolicy:vi.fn(), evaluateSnapshot:vi.fn(), listSeries:vi.fn(), listInsights:vi.fn() }));
vi.mock("../../lib/temporalMemory", async original => ({ ...await original<typeof memory>(), listSnapshots:vi.fn(), captureSnapshot:vi.fn(), getDiff:vi.fn() }));
const snap={snapshot_id:"tsnap_one",created_at:"2026-09-18T01:00:00Z",parent_snapshot_id:null};
const p={...api.defaultPolicy,policy_id:"apol_one",topic_id:"topic",created_at:snap.created_at};
beforeEach(()=>{
 vi.clearAllMocks();
 vi.mocked(api.listPolicies).mockResolvedValue({items:[],total:0,active_policy_id:null} as never);
 vi.mocked(memory.listSnapshots).mockResolvedValue({items:[],total:0} as never);
 vi.mocked(api.listSeries).mockResolvedValue({items:[],total:0} as never);
 vi.mocked(api.listInsights).mockResolvedValue({items:[],total:0} as never);
});
function mount(){render(<MemoryRouter><TemporalAnalysisPanel topicId="topic" /></MemoryRouter>);}
function active(){vi.mocked(api.listPolicies).mockResolvedValue({items:[p],active_policy_id:p.policy_id,total:1} as never);vi.mocked(memory.listSnapshots).mockResolvedValue({items:[snap],total:1} as never);}
it("no policy and no snapshot, real default creation and explicit activation",async()=>{
 mount();expect(await screen.findByText("활성 정책 없음")).toBeTruthy();
 expect((screen.getByRole("button",{name:"최신 스냅샷 분석"}) as HTMLButtonElement).disabled).toBe(true);
 vi.mocked(api.createPolicy).mockResolvedValue({policy:p,created:true} as never);
 vi.mocked(api.listPolicies).mockResolvedValue({items:[p],total:1,active_policy_id:null} as never);
 fireEvent.click(screen.getByRole("button",{name:"권장 기본 정책 만들기"}));
 expect(await screen.findByRole("button",{name:"정책 활성화"})).toBeTruthy();
 expect(api.activatePolicy).not.toHaveBeenCalled();
 vi.mocked(api.activatePolicy).mockResolvedValue({policy:p} as never);
 fireEvent.click(screen.getByRole("button",{name:"정책 활성화"}));
 await waitFor(()=>expect(api.activatePolicy).toHaveBeenCalledWith("topic",p.policy_id));
});
it("immutable new version form",async()=>{
 active();mount();await screen.findByText(p.policy_id,{exact:true});
 fireEvent.change(screen.getByLabelText("최소 논문 수"),{target:{value:"2"}});
 vi.mocked(api.createPolicy).mockResolvedValue({policy:{...p,policy_id:"apol_two"},created:true} as never);
 fireEvent.click(screen.getByRole("button",{name:"새 정책 버전 만들기"}));
 await waitFor(()=>expect(api.createPolicy).toHaveBeenCalledWith("topic",expect.objectContaining({minimum_paper_count:2})));
});
it.each([true,false])("manual analysis created=%s",async(created)=>{
 active();vi.mocked(api.evaluateSnapshot).mockResolvedValue({created,evaluation:{evaluation_id:"e"}} as never);mount();
 await screen.findByText(p.policy_id,{exact:true});
 expect(api.evaluateSnapshot).not.toHaveBeenCalled();
 const button=screen.getByRole("button",{name:"최신 스냅샷 분석"});button.focus();expect(document.activeElement).toBe(button);fireEvent.click(button);
 expect(await screen.findByText(created?"분석 기록을 저장했습니다.":"기존 분석 기록을 반환했습니다.")).toBeTruthy();
 expect(api.evaluateSnapshot).toHaveBeenCalledWith("topic",snap.snapshot_id,p.policy_id);
});
it.each(["INSUFFICIENT_DENOMINATOR","INSUFFICIENT_BASELINE","UNAVAILABLE","AVAILABLE"])("exact metric table %s",async(state)=>{
 active();vi.mocked(api.listSeries).mockResolvedValue({items:[{metric_point_id:"m",metric_id:"corpus.paper_count",state,numerator:null,denominator:null,count_value:state==="AVAILABLE"?3:null,unit:"COUNT",source_snapshot_id:snap.snapshot_id,snapshot_created_at:snap.created_at,policy_id:p.policy_id,formula_version:"v1",formula_registry_version:"M3B_METRIC_FORMULA_REGISTRY_V1"}],total:1} as never);
 mount();await screen.findByRole("table",{name:"정확한 측정값"});expect(screen.getByRole("table").textContent).toContain(state);
 if(state!=="AVAILABLE")expect(screen.getByRole("table").textContent).not.toContain("0%");
 fireEvent.change(screen.getByLabelText("측정 지표"),{target:{value:"coverage.reviewed_paper_ratio"}});
 await waitFor(()=>expect(api.listSeries).toHaveBeenLastCalledWith("topic","coverage.reviewed_paper_ratio",1,undefined));
});
it.each(["ACCEPTED","UNAVAILABLE","INSUFFICIENT_DATA","BELOW_POLICY_THRESHOLD"])("truthful insight %s",async(state)=>{
 active();vi.mocked(api.listInsights).mockResolvedValue({items:[{insight_record_id:"i",insight_kind:"REVIEW_COVERAGE_CHANGE",state,reason_code:"REASON",rendered_factual_text:"Review coverage changed from 0/1 to 1/1.",source_snapshot_id:snap.snapshot_id,baseline_snapshot_id:"before",source_entity_ids:["paper"],source_evidence_ids:[],metric_point_ids:["m"],policy_id:p.policy_id,formula_registry_version:"M3B_METRIC_FORMULA_REGISTRY_V1",template_version:"M3B_FACTUAL_INSIGHT_TEMPLATE_V1",paper_destinations:[],evidence_destinations:[]}],total:1} as never);
 mount();expect(await screen.findByText("Review coverage changed from 0/1 to 1/1.")).toBeTruthy();expect(screen.getByText(state,{exact:true})).toBeTruthy();
});
it.each([false,true])("independent policy error unavailable=%s",async(unavailable)=>{
 vi.mocked(api.listPolicies).mockRejectedValue(unavailable?new ApiError("offline",503,""):new Error("broken"));mount();
 expect(await screen.findByRole("alert")).toBeTruthy();expect(screen.queryByText("활성 정책 없음")).toBeNull();
 expect(await screen.findByText("저장된 분석 기록이 없습니다.")).toBeTruthy();
});
it("evaluation failure is an alert without fake success",async()=>{
 active();vi.mocked(api.evaluateSnapshot).mockRejectedValue(new Error("failure"));mount();await screen.findByText(p.policy_id,{exact:true});
 fireEvent.click(screen.getByRole("button",{name:"최신 스냅샷 분석"}));expect(await screen.findByRole("alert")).toBeTruthy();expect(screen.queryByText("분석 기록을 저장했습니다.")).toBeNull();
});
it.each(["12h","24h"] as const)("respects %s time preference",async(timeFormat)=>{
 active();localStorage.setItem(APPEARANCE_STORAGE_KEY,JSON.stringify({...DEFAULT_APPEARANCE,timeFormat}));
 document.cookie="xrml_appearance_v1=; max-age=0; path=/";
 render(<MemoryRouter><AppearanceProvider><TemporalAnalysisPanel topicId="topic"/></AppearanceProvider></MemoryRouter>);
 await screen.findByText(p.policy_id,{exact:true});
 expect(screen.getAllByText(formatTimestamp(snap.created_at,timeFormat,"ko-KR")).length).toBeGreaterThan(0);
 localStorage.removeItem(APPEARANCE_STORAGE_KEY);
});
it.each(["create","activate"])("policy mutation failure %s",async(kind)=>{
 if(kind==="activate")vi.mocked(api.listPolicies).mockResolvedValue({items:[p],total:1,active_policy_id:null} as never);
 vi.mocked(api.createPolicy).mockRejectedValue(new Error("create failure"));
 vi.mocked(api.activatePolicy).mockRejectedValue(new Error("activate failure"));
 mount();await screen.findByText("활성 정책 없음");
 fireEvent.click(screen.getByRole("button",{name:kind==="create"?"권장 기본 정책 만들기":"정책 활성화"}));
 expect(await screen.findByRole("alert")).toBeTruthy();
});
it("series and insights unavailable are independent from policies and empty",async()=>{
 active();vi.mocked(api.listSeries).mockRejectedValue(new ApiError("offline",503,""));
 vi.mocked(api.listInsights).mockRejectedValue(new Error("broken"));mount();
 await screen.findByText(p.policy_id,{exact:true});
 expect(await screen.findByText(/측정값 사용 불가/)).toBeTruthy();
 expect(await screen.findByText(/분석 기록 오류/)).toBeTruthy();
 expect(screen.queryByText("저장된 분석 기록이 없습니다.")).toBeNull();
});
it("busy evaluation prevents duplicate clicks and never evaluates on refresh",async()=>{
 active();let resolve!:(value:unknown)=>void;
 vi.mocked(api.evaluateSnapshot).mockReturnValue(new Promise(r=>{resolve=r as never;}));mount();await screen.findByText(p.policy_id,{exact:true});
 fireEvent.click(screen.getByRole("button",{name:"분석 자료 새로고침"}));await screen.findByText(p.policy_id,{exact:true});
 expect(api.evaluateSnapshot).not.toHaveBeenCalled();
 const button=screen.getByRole("button",{name:"최신 스냅샷 분석"});fireEvent.click(button);fireEvent.click(button);
 expect(api.evaluateSnapshot).toHaveBeenCalledTimes(1);expect((button as HTMLButtonElement).disabled).toBe(true);
 resolve({created:true,evaluation:{evaluation_id:"e"}});await screen.findByText("분석 기록을 저장했습니다.");
});

it("capture immediately selects the returned record for analysis without a second refresh",async()=>{
 active();vi.mocked(memory.listSnapshots).mockResolvedValue({items:[],total:0} as never);
 vi.mocked(memory.captureSnapshot).mockResolvedValue({snapshot:snap,created:true} as never);
 vi.mocked(api.evaluateSnapshot).mockResolvedValue({created:true,evaluation:{evaluation_id:"e"}} as never);
 render(<MemoryRouter><TemporalResearchPanel topicId="topic"/></MemoryRouter>);
 await screen.findByText("활성 정책: "+p.policy_id);
 const evaluate=screen.getByRole("button",{name:"선택 스냅샷 분석"}) as HTMLButtonElement;
 expect(evaluate.disabled).toBe(true);
 vi.mocked(memory.listSnapshots).mockResolvedValue({items:[snap],total:1} as never);
 fireEvent.click(screen.getByRole("button",{name:"현재 상태 스냅샷 만들기"}));
 await waitFor(()=>expect(evaluate.disabled).toBe(false));
 fireEvent.click(evaluate);
 await waitFor(()=>expect(api.evaluateSnapshot).toHaveBeenCalledWith("topic",snap.snapshot_id,p.policy_id));
});

it("historical selection drives explicit evaluation and scoped records, resetting pagination",async()=>{
 active();
 const recent={...snap,snapshot_id:"tsnap_recent",parent_snapshot_id:snap.snapshot_id};
 vi.mocked(memory.listSnapshots).mockResolvedValue({items:[recent,snap],total:2} as never);
 vi.mocked(memory.getDiff).mockResolvedValue({from_snapshot:snap,to_snapshot:recent,events:[],counts:{}} as never);
 vi.mocked(api.listInsights).mockResolvedValue({items:[],total:60} as never);
 vi.mocked(api.evaluateSnapshot).mockResolvedValue({created:false,evaluation:{evaluation_id:"e"}} as never);
 render(<MemoryRouter><TemporalResearchPanel topicId="topic"/></MemoryRouter>);
 await screen.findByLabelText("스냅샷 이력");
 fireEvent.click(screen.getByLabelText("선택 스냅샷 기록만 보기"));
 await waitFor(()=>expect(api.listInsights).toHaveBeenLastCalledWith("topic",1,recent.snapshot_id));
 fireEvent.click(screen.getByRole("button",{name:"다음 분석 기록"}));
 await waitFor(()=>expect(api.listInsights).toHaveBeenLastCalledWith("topic",2,recent.snapshot_id));
 fireEvent.change(screen.getByLabelText("스냅샷 이력"),{target:{value:snap.snapshot_id}});
 await waitFor(()=>expect(api.listInsights).toHaveBeenLastCalledWith("topic",1,snap.snapshot_id));
 expect(await screen.findByText("선택한 첫 스냅샷에는 이전 비교 기준이 없습니다.")).toBeTruthy();
 fireEvent.click(screen.getByRole("button",{name:"선택 스냅샷 분석"}));
 await waitFor(()=>expect(api.evaluateSnapshot).toHaveBeenCalledWith("topic",snap.snapshot_id,p.policy_id));
});

it("different policies have separate plots and visible policy identities",async()=>{
 active();
 const point={metric_id:"corpus.paper_count",state:"AVAILABLE",count_value:3,unit:"COUNT",source_snapshot_id:snap.snapshot_id,snapshot_created_at:snap.created_at};
 vi.mocked(api.listSeries).mockResolvedValue({items:[{...point,metric_point_id:"one",policy_id:"policy-one"},{...point,metric_point_id:"two",policy_id:"policy-two",state:"UNAVAILABLE"}],total:2} as never);
 mount();
 const table=await screen.findByRole("table",{name:"정확한 측정값"});
 expect(screen.getAllByRole("img",{name:/스냅샷 측정점/})).toHaveLength(2);
 expect(within(table).getByText("policy-one")).toBeTruthy();
 expect(within(table).getByText("policy-two")).toBeTruthy();
});
