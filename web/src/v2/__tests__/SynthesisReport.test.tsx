import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { SynthesisReportPanel } from "../views/SynthesisReportPanel";

vi.mock("../../lib/api",async original=>({...await original<typeof api>(),fetchJson:vi.fn()}));
const empty={version:null,latest_decision:null,candidate_digest:"binding",export_eligible:false};
const decision={decision_id:"decision",state:"accepted",note:"Read evidence",actor_id:"owner",created_at:"2026-09-27T00:00:00Z"};
const saved={version:{version_id:"version",created_at:decision.created_at,content:"Frozen report",content_sha256:"hash",acceptance_decision_id:"decision"},latest_decision:decision,candidate_digest:"binding",export_eligible:true};
beforeEach(()=>{vi.resetAllMocks();});

it("requires explicit acceptance, binds the candidate, and updates saved report state without closing history",async()=>{
 let accepted=false;
 vi.mocked(api.fetchJson).mockImplementation(async(url,options)=>{
  if(options?.method==="POST"){accepted=true;return {decision,created:true} as never;}
  if(url.includes("/decisions"))return {items:accepted?[decision]:[],total:accepted?1:0,page:1,limit:10,latest_id:accepted?"decision":null} as never;
  return (accepted?saved:empty) as never;
 });
 render(<SynthesisReportPanel topicId="topic" jobId="job"/>);
 await screen.findByText("현재 판단: 판단 전");
 expect(screen.getByRole("button",{name:"Markdown 내보내기"})).toHaveProperty("disabled",true);
 expect(vi.mocked(api.fetchJson).mock.calls.every(([,o])=>!o?.method)).toBe(true);
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByText("아직 사용자 판단이 없습니다.");
 fireEvent.change(screen.getByLabelText("검토 판단"),{target:{value:"accepted"}});
 fireEvent.change(screen.getByLabelText("판단 사유"),{target:{value:"Read evidence"}});
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await screen.findByText("1개 판단 기록");
 await screen.findByText("현재 판단: 근거 보고서로 채택");
 expect(screen.getByRole("button",{name:"근거 JSON 내보내기"})).toHaveProperty("disabled",false);
 const request=vi.mocked(api.fetchJson).mock.calls.find(([,o])=>o?.method==="POST")!;
 expect(request[0]).toBe("/api/topics/topic/local-synthesis/job/decisions");
 expect(JSON.parse(request[1]!.body as string)).toMatchObject({state:"accepted",expected_binding:"binding",expected_previous_id:null,note:"Read evidence"});
});

it("retains the same acceptance request identity after a lost response",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async(url,options)=>{
  if(options?.method==="POST")throw new Error("lost response");
  return (url.includes("/decisions")?{items:[],total:0,page:1,limit:10,latest_id:null}:empty) as never;
 });
 render(<SynthesisReportPanel topicId="topic" jobId="job"/>);
 await screen.findByText("현재 판단: 판단 전");
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByText("아직 사용자 판단이 없습니다.");
 fireEvent.change(screen.getByLabelText("검토 판단"),{target:{value:"accepted"}});
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await waitFor(()=>expect(vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST")).toHaveLength(2));
 const writes=vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST");
 expect(writes[0][1]?.body).toBe(writes[1][1]?.body);
});

it("does not download when the server rejects stale acceptance, and disables export after hold",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async url=>{
  if(url.includes("/export"))throw new Error("synthesis_export_not_accepted");
  return saved as never;
 });
 render(<SynthesisReportPanel topicId="topic" jobId="job"/>);
 await screen.findByText("현재 판단: 근거 보고서로 채택");
 fireEvent.click(screen.getByRole("button",{name:"Markdown 내보내기"}));
 await screen.findByRole("alert");
 const request=vi.mocked(api.fetchJson).mock.calls.find(([url])=>url.includes("/export"))!;
 expect(request[0]).toContain("version_id=version&decision_id=decision");
 vi.mocked(api.fetchJson).mockResolvedValue({...saved,latest_decision:{...decision,state:"held"},export_eligible:false});
 fireEvent.click(screen.getByRole("button",{name:"보고서·판단 새로고침"}));
 await screen.findByText("현재 판단: 보류");
 expect(screen.getByRole("button",{name:"Markdown 내보내기"})).toHaveProperty("disabled",true);
 expect(screen.getByLabelText("보존한 로컬 근거 보고서").textContent).toBe("Frozen report");
});

it("refuses export from stale displayed data when refreshing report status fails",async()=>{
 vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error("store unavailable"));
 render(<SynthesisReportPanel topicId="topic" jobId="job"/>);
 await screen.findByText("현재 판단: 근거 보고서로 채택");
 fireEvent.click(screen.getByRole("button",{name:"보고서·판단 새로고침"}));
 await screen.findByRole("alert");
 expect(screen.getByRole("button",{name:"Markdown 내보내기"})).toHaveProperty("disabled",true);
 expect(screen.getByText(/이전 표시는 현재 상태로 확인되지 않았습니다/)).toBeTruthy();
});
