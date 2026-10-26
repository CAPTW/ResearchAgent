import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ReviewResearchContext } from "../../lib/reviewPreparation";
import { formatTimestamp } from "../appearance";
import { SavedReviewResearchContext } from "../components/SavedReviewResearchContext";

const appearance = vi.hoisted(() => ({ timeFormat: "12h" as "12h" | "24h" }));
vi.mock("../AppearanceProvider", () => ({ useOptionalAppearance: () => ({ preferences: { timeFormat: appearance.timeFormat } }) }));

it("updates the saved topic timestamp with the appearance preference while preserving the publication date and context", () => {
  const context: ReviewResearchContext = {
    schema_version: "REVIEW_CONTEXT_V1", topic_title: "Saved title", topic_name: "Saved research question",
    paper_title: "Saved paper", authors: ["Saved Author"], published_at: "2026-08-01",
    metadata_digest: "a".repeat(64), topic_updated_at: "2026-09-27T09:23:45Z",
  };
  const original = JSON.stringify(context);
  const view = render(<SavedReviewResearchContext value={context}/>);
  expect(screen.getByText(`저장된 주제 수정 시각: ${formatTimestamp(context.topic_updated_at, "12h", "ko-KR")}`)).toBeTruthy();
  expect(screen.getByText("발행일: 2026-08-01")).toBeTruthy();
  expect(screen.queryByText(`저장된 주제 수정 시각: ${context.topic_updated_at}`)).toBeNull();

  appearance.timeFormat = "24h";
  view.rerender(<SavedReviewResearchContext value={context}/>);
  expect(screen.getByText(`저장된 주제 수정 시각: ${formatTimestamp(context.topic_updated_at, "24h", "ko-KR")}`)).toBeTruthy();
  expect(screen.getByText("발행일: 2026-08-01")).toBeTruthy();
  expect(JSON.stringify(context)).toBe(original);
});
