import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { LocalReportReader } from "../views/LocalReportReader";
import { SynthesisReportComparison } from "../views/SynthesisReportComparison";

vi.mock("../../lib/api",async original=>({...await original<typeof api>(),fetchJson:vi.fn()}));
beforeEach(()=>vi.resetAllMocks());
const summary=(id:string)=>({job_id:id,version_id:`version-${id}`,created_at:"2026-09-27T00:00:00Z",paper_count:2,input_digest:`input-${id}`,source_snapshot_id:`snapshot-${id}`,content_sha256:`hash-${id}`,latest_decision:{state:"held"}});
const comparison=(id:string)=>({
 before:summary(id),after:summary("current"),counts:{added:0,removed:1,changed:1,unchanged:0},input_context:[],
 coverage:[{metric:"selected_papers",before:3,after:2,delta:-1}],
 papers:[{paper_version_id:"p1",title:"Paper",status:"changed",fields:[
  {field:"abstract",before:"Old abstract",after:"New abstract"},
  {field:"evidence",before:[{locator:{page:1},text:"Original excerpt"}],after:[{locator:{page:2},text:"New excerpt"}]},
 ]}],
});

it("renders generated blocks while keeping source HTML, images, links and nested Markdown inert",()=>{
 const content='# Title\n\n## Coverage\n- Selected: 2\n- ID: `v1`\n\n### Abstract\n> <img src="https://example.org/pixel" onerror="alert(1)">\n> [unsafe](javascript:alert(1))\n> # Quoted heading\n\n````json\n{"marker":"```", "raw":"<script>bad()</script>"}\n````\n\nExact paragraph';
 const {container}=render(<LocalReportReader content={content}/>);
 expect(screen.getByRole("heading",{name:"Title"})).toBeTruthy();
 expect(screen.getByRole("heading",{name:"Coverage"})).toBeTruthy();
 expect(screen.getAllByRole("listitem")).toHaveLength(2);
 expect(container.querySelector("blockquote")?.textContent).toContain('# Quoted heading');
 expect(container.querySelectorAll("img,a,script,iframe")).toHaveLength(0);
 expect(container.querySelector("pre")?.textContent).toBe('{"marker":"```", "raw":"<script>bad()</script>"}');
 expect(screen.getByText("Exact paragraph")).toBeTruthy();
});

it("requires an explicit baseline and pages preserved reports without showing an invented zero difference",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async url=>{
  if(url.includes("comparison"))return comparison("older") as never;
  return {items:url.includes("page=2")?[summary("older")]:[summary("current")],total:11,page:url.includes("page=2")?2:1,limit:10} as never;
 });
 render(<SynthesisReportComparison topicId="topic" jobId="current"/>);
 await screen.findByText("11개 보존 보고서 · 1페이지");
 expect(screen.getByText(/비교 기준을 선택하기 전/)).toBeTruthy();
 expect(screen.queryByRole("table")).toBeNull();
 expect(screen.getByRole("button",{name:/현재 선택 · 2편/})).toHaveProperty("disabled",true);
 fireEvent.click(screen.getByRole("button",{name:"보존 보고서 다음 페이지"}));
 await screen.findByText("11개 보존 보고서 · 2페이지");
 fireEvent.click(screen.getByRole("button",{name:/비교 기준으로 선택/}));
 await screen.findByText("논문 버전: 추가 0 · 제외 1 · 기록 변경 1 · 같음 0");
 expect(screen.getByRole("table").textContent).toContain("선택 논문32-1");
 fireEvent.click(screen.getByText("기록 변경 · Paper"));
 fireEvent.click(screen.getByText("초록",{exact:true}));
 expect(screen.getByText("Old abstract")).toBeTruthy();
 expect(screen.getByText("New abstract")).toBeTruthy();
 expect(vi.mocked(api.fetchJson).mock.calls.some(([url])=>url.endsWith("current/report/comparison?from_job_id=older"))).toBe(true);
});

it("discards a late comparison when the baseline changes and clears stale results on failed refresh",async()=>{
 let resolveOld:(value:unknown)=>void=()=>{};
 let failed=false;
 vi.mocked(api.fetchJson).mockImplementation(async url=>{
  if(url.includes("from_job_id=a"))return new Promise(resolve=>{resolveOld=resolve;}) as never;
  if(url.includes("comparison")){if(failed)throw new Error("corrupt preserved report");return comparison("b") as never;}
  return {items:[summary("a"),summary("b")],total:2,page:1,limit:10} as never;
 });
 render(<SynthesisReportComparison topicId="topic" jobId="current"/>);
 await screen.findByText("2개 보존 보고서 · 1페이지");
 const choices=screen.getAllByRole("button",{name:/비교 기준으로 선택/});
 fireEvent.click(choices[0]);
 fireEvent.click(choices[1]);
 await screen.findByRole("table");
 await act(async()=>resolveOld(comparison("a")));
 expect(screen.getByText("선택한 비교 기준 작업: b")).toBeTruthy();
 expect(screen.queryByText("input-a",{exact:false})).toBeNull();
 failed=true;
 fireEvent.click(screen.getByRole("button",{name:"비교 목록·결과 새로고침"}));
 await screen.findByRole("alert");
 expect(screen.queryByRole("table")).toBeNull();
 expect(screen.getByText(/변화가 없는 것으로 해석할 수 없습니다/)).toBeTruthy();
});

it("shows list failure separately and retries without starting a comparison",async()=>{
 vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({items:[],total:0,page:1,limit:10});
 render(<SynthesisReportComparison topicId="topic" jobId="current"/>);
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"비교 목록·결과 새로고침"}));
 await screen.findByText("0개 보존 보고서 · 1페이지");
 expect(within(screen.getByRole("region")).queryByRole("table")).toBeNull();
 expect(vi.mocked(api.fetchJson).mock.calls.every(([url])=>!url.includes("comparison"))).toBe(true);
});
