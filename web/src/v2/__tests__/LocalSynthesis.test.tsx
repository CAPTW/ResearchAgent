import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as api from "../../lib/localSynthesis";
import * as productApi from "../../lib/api";
import { LocalSynthesisPanel } from "../views/LocalSynthesisPanel";
import { V2TopicWorkspaceScreen } from "../V2TopicWorkspaceScreen";

vi.mock("../../lib/localSynthesis");
vi.mock("../../lib/api",async original=>({...await original<typeof productApi>(),fetchJson:vi.fn(),getApiTopicDashboard:vi.fn(),getTopicProfiles:vi.fn(),getApiReports:vi.fn(),getTopicSchedules:vi.fn()}));
const job:api.SynthesisJob={job_id:"syn-one",state:"READY",created_at:"2026-09-27T00:00:00Z",actor_id:"owner",completed_steps:0,total_steps:3,input_digest:"input-digest",steps:[],candidate:null,
 inputs:{schema_version:"LOCAL_EVIDENCE_SYNTHESIS_V1",topic_id:"topic",topic_label:"Topic",source_snapshot_id:"snapshot",plan:{pipeline_version:"LOCAL_EXTRACTIVE_V1",shard_size:5,provider_mode:"disabled"},papers:[{paper_version_id:"version",canonical_paper_id:"paper",metadata_digest:"metadata",title:"Frozen paper",authors:[],abstract:"Original frozen abstract",review_state:"not_reviewed",evidence:[],rights:{state:"unknown",label:null},source_identity:{doi:"example"},review_source_run_id:null,source_text_digest:null}]}};
beforeEach(()=>{
 vi.resetAllMocks();
 vi.mocked(productApi.fetchJson).mockResolvedValue({version:null,latest_decision:null,candidate_digest:"binding",export_eligible:false});
 vi.mocked(api.listSynthesis).mockResolvedValue({items:[],total:0,page:1,limit:10});
 vi.mocked(api.getSynthesis).mockResolvedValue(structuredClone(job));
});

it("loads frozen input without executing and retries the same lost-response checkpoint",async()=>{
 vi.mocked(api.advanceSynthesis).mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({job:{...job,completed_steps:1},advanced:false});
 render(<LocalSynthesisPanel topicId="topic" initialJobId="syn-one"/>);
 await screen.findByText("수동 진행 가능 · 0 / 3 단계");
 expect(screen.getByText("Original frozen abstract")).toBeTruthy();
 expect(api.advanceSynthesis).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button",{name:"다음 한 단계 실행"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"다음 한 단계 실행"}));
 await screen.findByText("수동 진행 가능 · 1 / 3 단계");
 expect(api.advanceSynthesis).toHaveBeenNthCalledWith(1,"topic","syn-one",0);
 expect(api.advanceSynthesis).toHaveBeenNthCalledWith(2,"topic","syn-one",0);
});

it("reads selected frozen review inputs and past interpretations without fetching current sources", async () => {
 const receipt = { receipt_id: "saved-review", input_origin: "FIXTURE_SYNTHETIC", recorded_at: "2026-09-28T09:00:00Z", run_id: "run", paper_id: "paper", review_level: "full_text_grounded", packet: { selected_text_excerpts: [{ text: "Past input text", char_count: 15 }], input_scope: { schema_version: "AI_REVIEW_INPUT_SCOPE_V1", full_document_review: "NOT_ESTABLISHED" } }, draft: { contribution_summary: "Past model interpretation" } };
 const frozen = { ...job, inputs: { ...job.inputs, schema_version: "LOCAL_EVIDENCE_SYNTHESIS_V2", papers: [{ ...job.inputs.papers[0], review_input: { schema_version: "FROZEN_REVIEW_INPUT_V1", receipt, selected_component_ids: ["/selected_text_excerpts/0/text"] } }] } };
 vi.mocked(api.getSynthesis).mockResolvedValue(frozen as never);
 render(<LocalSynthesisPanel topicId="topic" initialJobId="syn-one"/>);
 await screen.findByText("고정한 검토 기록: saved-review");
 expect(screen.getByText("Past input text")).toBeTruthy();
 expect(screen.getByText("Past model interpretation")).toBeTruthy();
 expect(screen.getByText(/과거 검토 결과 · 원문 사실과 구분되는 해석/)).toBeTruthy();
 expect(screen.getByText(/전체 문서 검토: 확인되지 않음/)).toBeTruthy();
 expect(productApi.fetchJson).not.toHaveBeenCalled();
 expect(api.advanceSynthesis).not.toHaveBeenCalled();
});

it("pauses and resumes manually and refreshes persisted state",async()=>{
 vi.mocked(api.pauseSynthesis).mockResolvedValueOnce({...job,state:"PAUSED"}).mockResolvedValueOnce(job);
 render(<LocalSynthesisPanel topicId="topic" initialJobId="syn-one"/>);
 fireEvent.click(await screen.findByRole("button",{name:"진행 일시 중지"}));
 await screen.findByText("일시 중지 · 0 / 3 단계");
 expect(screen.queryByRole("button",{name:"다음 한 단계 실행"})).toBeNull();
 fireEvent.click(screen.getByRole("button",{name:"수동 진행 재개"}));
 await screen.findByText("수동 진행 가능 · 0 / 3 단계");
 expect(api.advanceSynthesis).not.toHaveBeenCalled();
 vi.mocked(api.getSynthesis).mockResolvedValue({...job,completed_steps:2});
 fireEvent.click(screen.getByRole("button",{name:"고정 초안 새로고침"}));
 await screen.findByText("수동 진행 가능 · 2 / 3 단계");
});

it("shows errors separately from empty lists and recovers using refresh",async()=>{
 vi.mocked(api.listSynthesis).mockRejectedValueOnce(new Error("store unavailable")).mockResolvedValueOnce({items:[],total:0,page:1,limit:10});
 render(<LocalSynthesisPanel topicId="topic"/>);
 await screen.findByRole("alert");
 expect(screen.queryByText("고정된 보고서 입력이 없습니다.")).toBeNull();
 fireEvent.click(screen.getByRole("button",{name:"근거 초안 목록 새로고침"}));
 await screen.findByText("고정된 보고서 입력이 없습니다.");
});

it("ignores late details after choosing a different frozen job",async()=>{
 let resolve!:(value:api.SynthesisJob)=>void;
 vi.mocked(api.getSynthesis).mockImplementationOnce(()=>new Promise(r=>{resolve=r;})).mockResolvedValueOnce({...job,job_id:"syn-two",state:"PAUSED"});
 const {rerender}=render(<LocalSynthesisPanel topicId="topic" initialJobId="syn-one"/>);
 await waitFor(()=>expect(api.getSynthesis).toHaveBeenCalledTimes(1));
 rerender(<LocalSynthesisPanel topicId="topic" initialJobId="syn-two"/>);
 await screen.findByText("일시 중지 · 0 / 3 단계");
 await act(async()=>resolve(job));
 expect(screen.getByText("작업: syn-two")).toBeTruthy();
 expect(screen.queryByText("작업: syn-one")).toBeNull();
});

it("labels a local candidate as extractive and contradictions as unassessed",async()=>{
 vi.mocked(api.getSynthesis).mockResolvedValue({...job,state:"CANDIDATE_READY",completed_steps:3,candidate:{schema_version:"v1",mode:"LOCAL_EXTRACTIVE_DRAFT",input_digest:"input-digest",markdown:"Preserved draft",references:[],coverage:{selected_papers:1,papers_with_abstract:1,papers_with_stored_excerpt:0,recorded_full_text_reviewed:0,contradiction_assessment:"NOT_ASSESSED",contradiction_count:null}}});
 render(<LocalSynthesisPanel topicId="topic" initialJobId="syn-one"/>);
 await screen.findByText("검토할 근거 초안");
 expect(screen.getByText(/모순 평가: 미평가/)).toBeTruthy();
 expect(screen.getByLabelText("고정 입력으로 만든 근거 초안").textContent).toBe("Preserved draft");
 expect(screen.queryByRole("button",{name:"다음 한 단계 실행"})).toBeNull();
});

it("keeps frozen drafts usable when the current report hub and dashboard fail",async()=>{
 vi.mocked(productApi.fetchJson).mockResolvedValue({items:[],total:0,page:1,limit:10});
 vi.mocked(productApi.getApiReports).mockRejectedValue(new Error("Current reports unavailable"));
 vi.mocked(productApi.getApiTopicDashboard).mockRejectedValue(new Error("Current dashboard unavailable"));
 vi.mocked(productApi.getTopicProfiles).mockResolvedValue({items:[]} as never);
 vi.mocked(productApi.getTopicSchedules).mockResolvedValue({items:[]} as never);
 render(<MemoryRouter initialEntries={["/topics/topic/reports?synthesis=syn-one"]}><Routes><Route path="/topics/:topicId/:tab" element={<V2TopicWorkspaceScreen/>}/></Routes></MemoryRouter>);
 await screen.findByText("수동 진행 가능 · 0 / 3 단계");
 expect(screen.getByText("Original frozen abstract")).toBeTruthy();
 await screen.findByText(/보존된 저널이 없습니다/);
 await waitFor(()=>expect(screen.getAllByRole("alert").some(node=>node.textContent?.includes("Current reports unavailable"))).toBe(true));
 expect(api.advanceSynthesis).not.toHaveBeenCalled();
});
