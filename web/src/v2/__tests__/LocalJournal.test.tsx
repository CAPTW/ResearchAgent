import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { LocalJournalPanel } from "../views/LocalJournalPanel";

vi.mock("../../lib/api",async original=>({...await original<typeof api>(),fetchJson:vi.fn()}));
const reference={citation_id:"ref-one",paper_version_id:"paper-v1",title:"Frozen paper",kind:"abstract",text:"Original frozen abstract",locator:null,source_identity:{doi:"doi-one"},review_state:"not_reviewed",rights:{state:"unknown"}};
const initial={items:[],total:0,page:1,limit:10,latest_version:null,input_digest:"digest",references:[reference]};
const version=(id:string,text="Human interpretation")=>({version_id:id,previous_id:null,actor_id:"owner",created_at:"2026-09-27T00:00:00Z",title:"Journal",entries:[{kind:"interpretation",text,citation_ids:[reference.citation_id]}],citations:[reference],markdown:"# Frozen journal",content_sha256:"hash",input_digest:"digest"});
const history=(id:string)=>({...initial,total:1,items:[version(id)],latest_version:version(id)});
beforeEach(()=>{vi.resetAllMocks();});

it("identifies selected review input excerpts distinctly while editing citations", async () => {
 vi.mocked(api.fetchJson).mockResolvedValue({ ...initial, references: [{ ...reference, kind: "review_input_excerpt" }] });
 await open();
 fireEvent.click(screen.getByText("문장 1 인용 선택"));
 expect(screen.getByRole("checkbox", { name: "Frozen paper · 선택한 검토 입력 발췌" })).toBeTruthy();
});

async function open(){
 render(<LocalJournalPanel topicId="topic" jobId="job"/>);
 expect(api.fetchJson).not.toHaveBeenCalled();
 fireEvent.click(screen.getByText("근거 연결 검토 초안",{exact:true}));
 await screen.findByLabelText("검토 초안 제목");
}
function fill(){
 fireEvent.change(screen.getByLabelText("검토 초안 제목"),{target:{value:"Journal"}});
 fireEvent.change(screen.getByLabelText("문장 1 내용"),{target:{value:"Human interpretation"}});
 fireEvent.click(screen.getByText("문장 1 인용 선택"));
 fireEvent.click(screen.getByRole("checkbox",{name:"Frozen paper · 초록"}));
}

it("restores a partial working copy explicitly and requires choosing a changed version baseline",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async(url)=>url.endsWith("journal-working-copy")?{head_id:"working",copy:{copy_id:"working",created_at:"2026-09-28T00:00:00Z",content:{base_version_id:null,title:"Recovered unfinished title",entries:[{kind:"question",text:"Partial question",citation_ids:[]}]}}} as never:history("v2") as never);
 await open();
 fireEvent.click(screen.getByText("작성 중 입력 보관·복구"));
 await screen.findByText(/마지막 확인한 보관 시각/);
 expect(screen.getByLabelText("검토 초안 제목")).toHaveProperty("value","Journal");
 fireEvent.click(screen.getByRole("button",{name:"현재 입력을 보관본으로 교체"}));
 expect(screen.getByLabelText("검토 초안 제목")).toHaveProperty("value","Recovered unfinished title");
 expect(screen.getByLabelText("문장 1 내용")).toHaveProperty("value","Partial question");
 fireEvent.click(screen.getByText("문장 1 인용 선택"));
 fireEvent.click(screen.getByRole("checkbox",{name:"Frozen paper · 초록"}));
 expect(screen.getByRole("button",{name:"검토 초안 새 버전 저장"})).toHaveProperty("disabled",true);
 fireEvent.click(screen.getByRole("button",{name:"최신본을 기준으로 현재 입력 유지"}));
 expect(screen.getByRole("button",{name:"검토 초안 새 버전 저장"})).toHaveProperty("disabled",false);
 expect(vi.mocked(api.fetchJson).mock.calls.every(([,options])=>!options?.method)).toBe(true);
});

it("binds each user sentence to selected frozen IDs and retains a saved version across panel collapse",async()=>{
 let saved=false;
 vi.mocked(api.fetchJson).mockImplementation(async(url,options)=>{
  if(options?.method==="POST"){saved=true;return {version:version("v1"),created:true} as never;}
  if(url.endsWith("/v1"))return version("v1") as never;
  return (saved?history("v1"):initial) as never;
 });
 await open();
 expect(screen.getByRole("button",{name:"검토 초안 새 버전 저장"})).toHaveProperty("disabled",true);
 fill();
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 새 버전 저장"}));
 await screen.findByText("검토 초안 이력 · 1개 버전");
 const request=vi.mocked(api.fetchJson).mock.calls.find(([,options])=>options?.method==="POST")!;
 expect(JSON.parse(request[1]!.body as string)).toMatchObject({expected_previous_id:null,expected_input_digest:"digest",title:"Journal",entries:[{kind:"interpretation",text:"Human interpretation",citation_ids:["ref-one"]}]});
 const savedVersion=await screen.findByRole("article",{name:"보존된 근거 연결 초안"});
 expect(within(savedVersion).getByText("Human interpretation")).toBeTruthy();
 expect(within(savedVersion).getByText("Original frozen abstract")).toBeTruthy();
 fireEvent.change(screen.getByLabelText("문장 1 내용"),{target:{value:"Unsubmitted revision"}});
 fireEvent.click(screen.getByText("근거 연결 검토 초안",{exact:true}));
 fireEvent.click(screen.getByText("근거 연결 검토 초안",{exact:true}));
 expect(screen.getByLabelText("문장 1 내용")).toHaveProperty("value","Unsubmitted revision");
});

it("retries identical request bytes after a lost response without clearing the user's text",async()=>{
 vi.mocked(api.fetchJson).mockImplementation(async(_,options)=>{if(options?.method==="POST")throw new Error("response lost");return initial as never;});
 await open();fill();
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 새 버전 저장"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 새 버전 저장"}));
 await waitFor(()=>expect(vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST")).toHaveLength(2));
 const writes=vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST");
 expect(writes[0][1]?.body).toBe(writes[1][1]?.body);
 expect(screen.getByLabelText("문장 1 내용")).toHaveProperty("value","Human interpretation");
});

it("preserves in-progress text on conflict and requires explicit selection of the refreshed predecessor",async()=>{
 let latest="v1",writes=0;
 vi.mocked(api.fetchJson).mockImplementation(async(_,options)=>{
  if(options?.method==="POST"){writes++;if(writes===1){latest="v2";throw new api.ApiError("conflict",409,"");}return {version:version("v3"),created:true} as never;}
  return history(latest) as never;
 });
 await open();
 fireEvent.change(screen.getByLabelText("문장 1 내용"),{target:{value:"My pending revision"}});
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 새 버전 저장"}));
 await screen.findByRole("alert");
 expect(screen.getByRole("button",{name:"검토 초안 새 버전 저장"})).toHaveProperty("disabled",true);
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 이력 새로고침"}));
 await screen.findByText(/최신본이 편집 기준과 다릅니다/);
 expect(screen.getByLabelText("문장 1 내용")).toHaveProperty("value","My pending revision");
 fireEvent.click(screen.getByRole("button",{name:"최신본을 기준으로 현재 입력 유지"}));
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 새 버전 저장"}));
 await waitFor(()=>expect(writes).toBe(2));
 const calls=vi.mocked(api.fetchJson).mock.calls.filter(([,o])=>o?.method==="POST");
 expect(JSON.parse(calls[1][1]!.body as string)).toMatchObject({expected_previous_id:"v2",entries:[{text:"My pending revision"}]});
});

it("keeps editing content when history refresh fails and never invents empty success",async()=>{
 vi.mocked(api.fetchJson).mockResolvedValueOnce(history("v1")).mockRejectedValueOnce(new Error("offline"));
 await open();
 fireEvent.change(screen.getByLabelText("문장 1 내용"),{target:{value:"Keep me"}});
 fireEvent.click(screen.getByRole("button",{name:"검토 초안 이력 새로고침"}));
 await screen.findByRole("alert");
 expect(screen.getByLabelText("문장 1 내용")).toHaveProperty("value","Keep me");
 expect(screen.getByText("검토 초안 이력 · 1개 버전")).toBeTruthy();
 expect(screen.getByLabelText("검토 초안 제목").closest("fieldset")!.disabled).toBe(true);
});

it("does not carry an old job's late history or entered text into another job",async()=>{
 let resolve!:(data:unknown)=>void;
 vi.mocked(api.fetchJson).mockImplementationOnce(()=>new Promise(r=>{resolve=r;})).mockResolvedValue(initial);
 const {rerender}=render(<LocalJournalPanel topicId="topic" jobId="old"/>);
 fireEvent.click(screen.getByText("근거 연결 검토 초안",{exact:true}));
 await waitFor(()=>expect(api.fetchJson).toHaveBeenCalledTimes(1));
 rerender(<LocalJournalPanel topicId="topic" jobId="new"/>);
 await screen.findByText("검토 초안 이력 · 0개 버전");
 await act(async()=>resolve(history("old-version")));
 expect(screen.getByLabelText("검토 초안 제목")).toHaveProperty("value","");
 expect(screen.queryByText(/old-version/)).toBeNull();
});

it("explains why no citation-bearing draft can be saved when frozen text is absent",async()=>{
 vi.mocked(api.fetchJson).mockResolvedValue({...initial,references:[]});
 render(<LocalJournalPanel topicId="topic" jobId="job"/>);
 fireEvent.click(screen.getByText("근거 연결 검토 초안",{exact:true}));
 await screen.findByText(/인용할 초록·발췌문이 없습니다/);
 expect(screen.queryByRole("button",{name:"검토 초안 새 버전 저장"})).toBeNull();
});
