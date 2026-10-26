import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReportVersionPanel } from "../views/ReportVersionPanel";
import { V2ReportsView } from "../views/V2ReportsView";

vi.mock("../../lib/api",async original=>({...await original<typeof api>(),getReportVersions:vi.fn(),getReportVersionDetail:vi.fn(),getReportVersionDiff:vi.fn()}));
const report={id:"report",runId:"run",topicName:"Local topic",title:"Local journal",status:"completed",completedVersionCount:2,sourceProviders:[],createdAt:"2026-09-27T00:00:00Z"} as api.ApiReportHubItem;
const versions=[1,2].map(n=>({version_id:`v${n}`,version_number:n,created_at:report.createdAt,content_hash:`hash-${n}`,source:"local_export"} as api.ReportVersion));
beforeEach(()=>{
 vi.resetAllMocks();
 vi.mocked(api.getReportVersions).mockResolvedValue({items:versions,total:2,warnings:[],run_id:"run"});
 vi.mocked(api.getReportVersionDetail).mockImplementation(async(_run,id)=>({run_id:"run",version:versions.find(v=>v.version_id===id)!,content:`Saved body ${id}`,content_integrity:"SHA256_VERIFIED",warnings:[]}));
 vi.mocked(api.getReportVersionDiff).mockResolvedValue({run_id:"run",ready:false,mode:"completed_versions",empty_reason:"Snapshot unavailable",warnings:[],summary:{added_sections:0,removed_sections:0,changed_sections:0,unchanged_sections:0},section_diffs:[]});
});

it("reads exact saved versions and describes an unavailable comparison without zero-change claims",async()=>{
 render(<ReportVersionPanel report={report} onClose={vi.fn()}/>);
 await screen.findByText("Saved body v2");
 fireEvent.change(screen.getByLabelText("읽을 보고서 버전"),{target:{value:"v1"}});
 await screen.findByText("Saved body v1");
 expect(api.getReportVersionDetail).toHaveBeenLastCalledWith("run","v1");
 fireEvent.change(screen.getByLabelText("비교 기준 버전"),{target:{value:"v2"}});
 await screen.findByText("Snapshot unavailable");
 expect(api.getReportVersionDiff).toHaveBeenCalledWith("run",{from_version_id:"v2",to_version_id:"v1"});
 expect(screen.queryByText(/변경 0/)).toBeNull();
});

it("keeps damaged snapshot and load errors visible and retries the same selection",async()=>{
 vi.mocked(api.getReportVersionDetail).mockRejectedValueOnce(new Error("offline"));
 render(<ReportVersionPanel report={report} onClose={vi.fn()}/>);
 await screen.findByRole("alert");
 vi.mocked(api.getReportVersionDetail).mockResolvedValue({run_id:"run",version:versions[1],content:null,content_integrity:"DIGEST_MISMATCH",warnings:["Stored hash differs"]});
 fireEvent.click(screen.getByRole("button",{name:"보고서 버전 새로고침"}));
 await screen.findByText("Stored hash differs");
 expect(screen.getByText("이 버전의 본문을 표시할 수 없습니다.")).toBeTruthy();
 expect(screen.queryByLabelText("저장된 Markdown 본문")).toBeNull();
});

it("ignores a delayed detail response after a different version is selected",async()=>{
 let resolveOld!:(value:api.ReportVersionDetailResponse)=>void;
 vi.mocked(api.getReportVersionDetail).mockImplementation((_run,id)=>id==="v2"?new Promise(resolve=>{resolveOld=resolve;}):Promise.resolve({run_id:"run",version:versions[0],content:"Earlier body",content_integrity:"SHA256_VERIFIED",warnings:[]}));
 render(<ReportVersionPanel report={report} onClose={vi.fn()}/>);
 await waitFor(()=>expect(api.getReportVersionDetail).toHaveBeenCalledWith("run","v2"));
 fireEvent.change(screen.getByLabelText("읽을 보고서 버전"),{target:{value:"v1"}});
 await screen.findByText("Earlier body");
 resolveOld({run_id:"run",version:versions[1],content:"Late obsolete body",warnings:[]});
 await waitFor(()=>expect(screen.queryByText("Late obsolete body")).toBeNull());
});

it("shows every returned report and opens its version reader with keyboard focus",async()=>{
 const items=Array.from({length:7},(_,n)=>({...report,id:String(n),runId:`run-${n}`,title:`Journal ${n}`}));
 render(<V2ReportsView reports={{items,total:7,warnings:[]}} schedules={[]} busyAction={null} busyScheduleId={null} onGenerateReport={vi.fn()} onToggleSchedule={vi.fn()} resolveReportUrl={()=>null}/>);
 const open=screen.getByRole("button",{name:"Journal 6 저장 버전 열기"});
 fireEvent.click(open);
 await screen.findByText("Saved body v2");
 expect(document.activeElement).toBe(screen.getByRole("region",{name:"보고서 저장 버전"}));
 expect(api.getReportVersions).toHaveBeenCalledWith("run-6");
 fireEvent.click(screen.getByRole("button",{name:"보고서 닫기"}));
 expect(document.activeElement).toBe(open);
});
