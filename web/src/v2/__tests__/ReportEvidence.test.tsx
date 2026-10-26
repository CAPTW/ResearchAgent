import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReportEvidencePanel } from "../views/ReportEvidencePanel";

vi.mock("../../lib/api",async original=>({...await original<typeof api>(),fetchJson:vi.fn()}));
const detail:api.ReportVersionDetailResponse={run_id:"run",version:{version_id:"v1"} as api.ReportVersion,content:"Saved report",warnings:[],review_binding:"saved-binding",review_eligible:true,
 citations:{status:"VERIFIED",warnings:[],items:[{citation_id:"c1",section_id:"section",section_title:"Evidence",claim_text:"Saved claim",snippet:"Saved excerpt",artifact_path:"https://example.test/source",provenance:{}}]}};
beforeEach(()=>{vi.resetAllMocks();vi.mocked(api.fetchJson).mockResolvedValue({items:[],total:0,limit:10,page:1,latest_id:null});});

it("browses saved citation text without following mutable sources",()=>{
 render(<ReportEvidencePanel detail={detail}/>);
 fireEvent.click(screen.getByText("이 버전에 보존된 인용"));
 expect(screen.getByText("Saved excerpt")).toBeTruthy();
 expect(screen.getByText("주장: Saved claim")).toBeTruthy();
 expect(screen.queryByRole("link")).toBeNull();
 expect(api.fetchJson).not.toHaveBeenCalled();
});

it("records an explicit version decision with its binding and preserves retry identity",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async(_url,options)=>{
  if(options?.method==="POST")throw new Error("response lost");
  return {items:[],total:0,limit:10,page:1,latest_id:null} as never;
 });
 render(<ReportEvidencePanel detail={detail}/>);
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByText("아직 사용자 판단이 없습니다.");
 fireEvent.change(screen.getByLabelText("검토 판단"),{target:{value:"accepted"}});
 fireEvent.change(screen.getByLabelText("판단 사유"),{target:{value:"Read saved evidence"}});
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await waitFor(()=>expect(vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST")).toHaveLength(2));
 const writes=vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST");
 expect(writes[0][0]).toBe("/runs/run/report/versions/v1/decisions");
 expect(writes[0][1]?.body).toBe(writes[1][1]?.body);
 expect(JSON.parse(writes[0][1]!.body as string)).toMatchObject({expected_binding:"saved-binding",state:"accepted",note:"Read saved evidence"});
});

it("shows missing evidence honestly and prevents accepting a damaged version",async()=>{
 render(<ReportEvidencePanel detail={{...detail,review_eligible:false,citations:{status:"DIGEST_MISMATCH",items:[],warnings:[]}}}/>);
 fireEvent.click(screen.getByText("이 버전에 보존된 인용"));
 expect(screen.getByText(/버전 연결이 일치하지 않습니다/)).toBeTruthy();
 expect(screen.queryByText("Saved excerpt")).toBeNull();
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByText("아직 사용자 판단이 없습니다.");
 expect(screen.getByRole("option",{name:"이 보고서 버전 채택"})).toHaveProperty("disabled",true);
 fireEvent.change(screen.getByLabelText("검토 판단"),{target:{value:"accepted"}});
 expect(screen.getByRole("button",{name:"판단 저장"})).toHaveProperty("disabled",true);
});
