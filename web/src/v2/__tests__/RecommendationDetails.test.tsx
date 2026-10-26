import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { RecommendationDetails } from "../views/RecommendationDetails";
import type { ApiTopicRecommendationsResponse } from "../../lib/api";

it("shows every returned candidate, low confidence, evidence and coverage limits", () => {
 const items = Array.from({length:6}, (_, n) => ({id:String(n),title:`Candidate ${n}`,type:"gap",confidence:"low",
  coverageWarning:"Abstract only",supportingPaperIds:["paper"],evidenceRefs:[{paper_id:"paper",review_level:"abstract_only",quoted_or_summarized_basis:"Observed limitation"}]}));
 render(<RecommendationDetails recommendations={{items,coverageWarning:"1 of 10 full texts"} as ApiTopicRecommendationsResponse}/>);
 expect(screen.getByText(/Candidate 5/)).toBeTruthy();
 expect(screen.getAllByText(/근거 신뢰도 낮음/)).toHaveLength(6);
 expect(screen.getAllByText("Observed limitation")).toHaveLength(6);
 expect(screen.getByText(/1 of 10 full texts/)).toBeTruthy();
 expect(screen.queryByText(/근거 신뢰도 중간/)).toBeNull();
});

it("distinguishes unavailable from no candidates", () => {
 const {rerender} = render(<RecommendationDetails recommendations={null}/>);
 expect(screen.getByText("추천 자료를 사용할 수 없습니다.")).toBeTruthy();
 rerender(<RecommendationDetails recommendations={{items:[]} as never}/>);
 expect(screen.getByText("현재 자료에서 생성된 연구 공백·주제 후보가 없습니다.")).toBeTruthy();
});
