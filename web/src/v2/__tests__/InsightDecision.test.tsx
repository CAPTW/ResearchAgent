import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { InsightDecisionPanel } from "../views/InsightDecisionPanel";
import * as api from "../../lib/temporalAnalysis";
import { ApiError } from "../../lib/api";

vi.mock("../../lib/temporalAnalysis", () => ({ listDecisions: vi.fn(), decideInsight: vi.fn() }));
const insight = { insight_record_id: "insight", state: "ACCEPTED" } as api.Insight;
const empty = {items: [], total: 0, latest_id: null, limit: 10, page: 1};
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.listDecisions).mockResolvedValue(empty); });
async function mount() {
 render(<InsightDecisionPanel topicId="topic" insight={insight}/>);
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByText("아직 사용자 판단이 없습니다.");
}
it("loads on demand and stores the user's decision independently of analysis", async () => {
 await mount();
 fireEvent.change(screen.getByLabelText("검토 판단"), {target: {value: "accepted"}});
 fireEvent.change(screen.getByLabelText("판단 사유"), {target: {value: "Read evidence first"}});
 vi.mocked(api.decideInsight).mockResolvedValue({created:true} as never);
 fireEvent.click(screen.getByRole("button", {name: "판단 저장"}));
 await screen.findByText("판단을 저장했습니다.");
 expect(api.decideInsight).toHaveBeenCalledWith("topic", "insight", expect.objectContaining({state:"accepted", note:"Read evidence first", expected_previous_id:null}));
});
it("retries a lost response with the same key and note", async () => {
 await mount();
 vi.mocked(api.decideInsight).mockRejectedValueOnce(new Error("connection lost")).mockResolvedValueOnce({created:false} as never);
 fireEvent.click(screen.getByRole("button", {name: "판단 저장"}));
 await screen.findByRole("alert");
 fireEvent.click(screen.getByRole("button", {name: "판단 저장"}));
 await screen.findByText("판단을 저장했습니다.");
 expect(vi.mocked(api.decideInsight).mock.calls[0]).toEqual(vi.mocked(api.decideInsight).mock.calls[1]);
});
it("requires history refresh on conflict without discarding the draft", async () => {
 await mount();
 fireEvent.change(screen.getByLabelText("판단 사유"), {target:{value:"preserve this"}});
 vi.mocked(api.decideInsight).mockRejectedValue(new ApiError("decision_conflict",409,""));
 fireEvent.click(screen.getByRole("button", {name:"판단 저장"}));
 await screen.findByRole("alert");
 expect((screen.getByRole("button",{name:"판단 저장"}) as HTMLButtonElement).disabled).toBe(true);
 vi.mocked(api.listDecisions).mockResolvedValue({...empty, latest_id:"newer"});
 fireEvent.click(screen.getByRole("button",{name:"판단 이력 새로고침"}));
 await waitFor(() => expect((screen.getByRole("button",{name:"판단 저장"}) as HTMLButtonElement).disabled).toBe(false));
 expect((screen.getByLabelText("판단 사유") as HTMLTextAreaElement).value).toBe("preserve this");
 vi.mocked(api.decideInsight).mockResolvedValue({created:true} as never);
 fireEvent.click(screen.getByRole("button",{name:"판단 저장"}));
 await waitFor(() => expect(api.decideInsight).toHaveBeenLastCalledWith("topic","insight",expect.objectContaining({expected_previous_id:"newer"})));
});
it("does not turn a failed history read into an empty success", async () => {
 vi.mocked(api.listDecisions).mockRejectedValue(new Error("offline"));
 render(<InsightDecisionPanel topicId="topic" insight={insight}/>);
 fireEvent.click(screen.getByText("검토 판단과 이력"));
 await screen.findByRole("alert");
 expect(screen.queryByText("아직 사용자 판단이 없습니다.")).toBeNull();
 expect((screen.getByRole("button",{name:"판단 저장"}) as HTMLButtonElement).disabled).toBe(true);
});
