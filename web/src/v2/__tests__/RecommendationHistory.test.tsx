import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError, getApiTopicRecommendations, type ApiTopicRecommendationsResponse } from "../../lib/api";
import * as api from "../../lib/recommendationVersions";
import { RecommendationHistoryPanel } from "../views/RecommendationHistoryPanel";

vi.mock("../../lib/recommendationVersions");
vi.mock("../../lib/api",async importOriginal=>({...await importOriginal<typeof import("../../lib/api")>(),getApiTopicRecommendations:vi.fn()}));
const item={id:"gap",type:"gap",title:"Local gap",versionId:"version-a",confidence:"low",evidenceRefs:[{paper_id:"paper",review_level:"abstract_only",quoted_or_summarized_basis:"Original evidence"}]};
const current={topicId:"topic",items:[item]} as ApiTopicRecommendationsResponse;
const version={topic_id:"topic",candidate_id:"gap",candidate_type:"gap",version_id:"version-a",created_at:"2026-09-27T00:00:00Z",actor_id:"owner",content:{generator_version:"baseline-v1",display:item,coverage_warning:"Partial coverage",candidate:{}}} as api.RecommendationVersion;
const empty={items:[],total:0,page:1,limit:10};
beforeEach(()=>{
 vi.resetAllMocks();
 vi.mocked(api.listRecommendationVersions).mockResolvedValue(empty);
 vi.mocked(api.listRecommendationDecisions).mockResolvedValue({...empty,latest_id:null});
 vi.mocked(getApiTopicRecommendations).mockResolvedValue(current);
});

it("saves the displayed version and reopens its exact evidence and decision target",async()=>{
 render(<RecommendationHistoryPanel topicId="topic" recommendations={current}/>);
 await screen.findByText("저장된 추천 버전이 없습니다.");
 fireEvent.click(screen.getByText(/Local gap/));
 vi.mocked(api.captureRecommendationVersion).mockResolvedValue({version,created:true});
 vi.mocked(api.listRecommendationVersions).mockResolvedValue({...empty,items:[version],total:1});
 fireEvent.click(screen.getByRole("button",{name:"이 버전 저장"}));
 await screen.findByText("1개 저장 버전");
 expect(api.captureRecommendationVersion).toHaveBeenCalledWith("topic",item);
 const saved=within(screen.getByRole("article",{name:"저장된 추천: Local gap"}));
 expect(saved.getByText(/저장 당시 로컬 자료/)).toBeTruthy();
 expect(saved.getByText("Original evidence")).toBeTruthy();
 fireEvent.click(saved.getByText("검토 판단과 이력"));
 await saved.findByText("아직 사용자 판단이 없습니다.");
 vi.mocked(api.decideRecommendation).mockResolvedValue({created:true} as never);
 fireEvent.change(saved.getByLabelText("판단 사유"),{target:{value:"Review this version"}});
 fireEvent.click(saved.getByRole("button",{name:"판단 저장"}));
 await waitFor(()=>expect(api.decideRecommendation).toHaveBeenCalledWith("topic","version-a",expect.objectContaining({note:"Review this version"})));
});

it("retains a lost-response capture identity and requires changed output to be reviewed",async()=>{
 render(<RecommendationHistoryPanel topicId="topic" recommendations={current}/>);
 fireEvent.click(screen.getByText(/Local gap/));
 vi.mocked(api.captureRecommendationVersion).mockRejectedValueOnce(new Error("response lost"));
 fireEvent.click(screen.getByRole("button",{name:"이 버전 저장"}));
 await screen.findByRole("alert");
 vi.mocked(api.captureRecommendationVersion).mockRejectedValueOnce(new ApiError("recommendation_changed",409,""));
 fireEvent.click(screen.getByRole("button",{name:"이 버전 저장"}));
 await screen.findByText(/추천 내용이 변경되었습니다/);
 expect(api.captureRecommendationVersion).toHaveBeenNthCalledWith(2,"topic",item);
 const changed={...item,versionId:"version-b",title:"Changed gap"};
 vi.mocked(getApiTopicRecommendations).mockResolvedValue({...current,items:[changed]} as ApiTopicRecommendationsResponse);
 fireEvent.click(screen.getByRole("button",{name:"현재 추천 새로고침"}));
 await screen.findByText(/Changed gap/);
 expect(screen.queryByText(/Local gap/)).toBeNull();
 fireEvent.click(screen.getByText(/Changed gap/));
 vi.mocked(api.captureRecommendationVersion).mockResolvedValue({version,created:false});
 fireEvent.click(screen.getByRole("button",{name:"이 버전 저장"}));
 await waitFor(()=>expect(api.captureRecommendationVersion).toHaveBeenLastCalledWith("topic",changed));
});

it("shows saved versions when current generation is unavailable and recovers history errors",async()=>{
 vi.mocked(api.listRecommendationVersions).mockRejectedValueOnce(new Error("database unavailable"));
 render(<RecommendationHistoryPanel topicId="topic" recommendations={null}/>);
 await screen.findByRole("alert");
 expect(screen.queryByText("저장된 추천 버전이 없습니다.")).toBeNull();
 vi.mocked(api.listRecommendationVersions).mockResolvedValue({...empty,items:[version],total:1});
 fireEvent.click(screen.getByRole("button",{name:"추천 이력 새로고침"}));
 await screen.findByRole("article",{name:"저장된 추천: Local gap"});
 expect(screen.getByText("추천 자료를 사용할 수 없습니다.")).toBeTruthy();
});

it("separates decisions for versions with the same mutable candidate ID and paginates saved history",async()=>{
 vi.mocked(api.listRecommendationVersions).mockResolvedValue({...empty,items:[version,{...version,version_id:"version-b"}],total:11});
 render(<RecommendationHistoryPanel topicId="topic" recommendations={{...current,items:[]}}/>);
 const cards=await screen.findAllByRole("article",{name:"저장된 추천: Local gap"});
 for(const card of cards)fireEvent.click(within(card).getByText("검토 판단과 이력"));
 await waitFor(()=>{
  expect(api.listRecommendationDecisions).toHaveBeenCalledWith("topic","version-a",1);
  expect(api.listRecommendationDecisions).toHaveBeenCalledWith("topic","version-b",1);
 });
 fireEvent.click(screen.getByRole("button",{name:"다음 추천 이력"}));
 await waitFor(()=>expect(api.listRecommendationVersions).toHaveBeenCalledWith("topic",2));
});

it("does not display a late recommendation response belonging to the previous topic",async()=>{
 render(<RecommendationHistoryPanel topicId="other-topic" recommendations={current}/>);
 await screen.findByText("저장된 추천 버전이 없습니다.");
 expect(screen.getByText("추천 자료를 사용할 수 없습니다.")).toBeTruthy();
 expect(screen.queryByText(/Local gap/)).toBeNull();
 expect(api.listRecommendationVersions).toHaveBeenCalledWith("other-topic",1);
});
