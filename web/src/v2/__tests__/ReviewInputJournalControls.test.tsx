import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewInputJournal } from "../components/ReviewInputJournal";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/local-synthesis/job";
const summary = "저장된 분석으로 Journal 후보 만들기", saveLabel = "선택한 항목을 Journal 후보로 저장";
const component = { component_id: "/contribution_summary", label: "기여 요약", text: "원문 그대로 e\u0301한🙂\n<img src='https://invalid.test'>", kind: "interpretation", section: "overview", eligible: true, support: "NOT_ASSESSED", ineligible_reason: null };
const unsupported = { ...component, component_id: "/unsupported_claims/0", label: "근거 미확인 주장 1", text: "원 응답이 근거 미확인으로 표시한 주장", section: "limitations", support: "NOT_ESTABLISHED" };
const input = { receipt_id: "receipt-one", paper_version_id: "paper-one", input_origin: "PROVIDER_RESPONSE", model: "fixture-model", validation_status: "validated", components: [component, unsupported] };
const packet = { packet_id: "packet-one", inputs: [input] };
const candidate = (selected = [component]) => ({
  schema_version: "REVIEW_INPUT_JOURNAL_CANDIDATE_V1", origin: "PRESERVED_REVIEW_DERIVATION", candidate_id: "candidate-one",
  preserved_at: "2026-10-15T00:00:00Z", preserved_by: "owner", selected_component_ids: selected.map(c => c.component_id),
  source: { receipt_id: input.receipt_id, paper_version_id: input.paper_version_id, input_origin: input.input_origin, model: input.model, validation_status: input.validation_status },
  packet: { packet_id: packet.packet_id }, raw_json: "{}", raw_sha256: "a".repeat(64),
  proposal: { title: "보존된 분석 후보", sections: ["overview", "limitations", "questions"].map(kind => ({ kind,
    entries: selected.filter(c => c.section === kind).map(c => ({ kind: c.kind, text: c.text, citation_ids: ["citation-one"] })) })).filter(s => s.entries.length) },
  component_bindings: ["overview", "limitations", "questions"].flatMap(section => selected.filter(c => c.section === section)
    .map((c, position) => ({ section, position, component_id: c.component_id, label: c.label, support: c.support }))),
  citations: [{ citation_id: "citation-one", kind: "review_input_excerpt", title: "논문", text: "고정 근거" }],
});
const onSaved = vi.fn(), onLocked = vi.fn();
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
async function open() { fireEvent.click(screen.getByText(summary)); await screen.findByLabelText("후보에 사용할 저장 분석"); }
async function select() { await open(); fireEvent.change(screen.getByLabelText("후보에 사용할 저장 분석"), { target: { value: input.receipt_id } }); }
async function choose() { await select(); fireEvent.click(screen.getByRole("checkbox", { name: /기여 요약/ })); }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(packet); });

it("loads only on explicit disclosure, requires one input and components, and preserves inert original text", async () => {
  render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>);
  expect(api.fetchJson).not.toHaveBeenCalled(); await open(); expect(api.fetchJson).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("후보에 사용할 저장 분석")).toHaveProperty("value", ""); expect(screen.queryByRole("checkbox")).toBeNull();
  fireEvent.change(screen.getByLabelText("후보에 사용할 저장 분석"), { target: { value: input.receipt_id } });
  expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", true);
  expect(screen.getByRole("checkbox", { name: /기여 요약/ })).toHaveProperty("checked", false); expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByText((_text, node) => node?.tagName === "PRE" && node.textContent === component.text)).toBeTruthy(); expect(screen.getByText(/원 응답에서 근거 미확인으로 표시된 주장/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it("saves exact selected components in source order with no new generation and opens only the matching returned candidate", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockResolvedValueOnce({ candidate: candidate([component, unsupported]), created: true });
  render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>); await select();
  fireEvent.click(screen.getByRole("checkbox", { name: /근거 미확인 주장 1/ })); fireEvent.click(screen.getByRole("checkbox", { name: /기여 요약/ }));
  fireEvent.click(screen.getByRole("button", { name: saveLabel })); await waitFor(() => expect(onSaved).toHaveBeenCalledWith("candidate-one"));
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(`${root}/journal-review-input-candidates`);
  expect(JSON.parse(String(posts()[0][1]?.body))).toEqual({ expected_packet_id: "packet-one", receipt_id: "receipt-one", component_ids: [component.component_id, unsupported.component_id] });
  expect(screen.getByRole("checkbox", { name: /기여 요약/ })).toHaveProperty("checked", true);
});

it("replays the exact immutable command after a lost response and accepts duplicate storage without another selection", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockRejectedValueOnce(new api.ApiError("lost", 503, "")).mockResolvedValueOnce({ candidate: candidate(), created: false });
  render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>); await choose(); fireEvent.click(screen.getByRole("button", { name: saveLabel }));
  const retry = await screen.findByRole("button", { name: "같은 Journal 후보 저장 다시 확인" });
  expect(screen.getByRole("checkbox", { name: /기여 요약/ })).toHaveProperty("disabled", true); expect(screen.getByLabelText("후보에 사용할 저장 분석")).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "저장된 분석 항목 다시 확인" })).toHaveProperty("disabled", true); expect(onSaved).not.toHaveBeenCalled();
  fireEvent.click(retry); await waitFor(() => expect(onSaved).toHaveBeenCalledWith("candidate-one")); expect(posts()).toHaveLength(2);
  expect(posts()[1][1]?.body).toBe(posts()[0][1]?.body); expect(screen.getByText(/이미 보존된 같은 후보/)).toBeTruthy();
});

it.each(["packet", "receipt", "component", "text", "support"])("does not promote a mismatched %s ACK and preserves exact retry", async mismatch => {
  const wrong = candidate();
  if (mismatch === "packet") wrong.packet.packet_id = "other";
  if (mismatch === "receipt") wrong.source.receipt_id = "other";
  if (mismatch === "component") wrong.selected_component_ids = ["other"];
  if (mismatch === "text") wrong.proposal.sections[0].entries[0].text = "rewritten";
  if (mismatch === "support") wrong.component_bindings[0].support = "NOT_ESTABLISHED";
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockResolvedValueOnce({ candidate: wrong, created: true });
  render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>); await choose(); fireEvent.click(screen.getByRole("button", { name: saveLabel }));
  await screen.findByRole("button", { name: "같은 Journal 후보 저장 다시 확인" }); expect(onSaved).not.toHaveBeenCalled(); expect(posts()).toHaveLength(1);
});

it("retains selection after a rejected packet and requires explicit reload before another new save", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockRejectedValueOnce(new api.ApiError("journal_packet_mismatch", 409, "")).mockResolvedValueOnce(packet);
  render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>); await choose(); fireEvent.click(screen.getByRole("button", { name: saveLabel }));
  await screen.findByRole("alert"); expect(screen.getByRole("checkbox", { name: /기여 요약/ })).toHaveProperty("checked", true); expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", true);
  expect(screen.queryByRole("button", { name: "같은 Journal 후보 저장 다시 확인" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "저장된 분석 항목 다시 확인" })); await waitFor(() => expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", false));
  expect(posts()).toHaveLength(1);
});

it.each(["BLANK_TEXT", "TEXT_TOO_LONG", "NO_SELECTED_CITED_EVIDENCE", "CITATION_LIMIT_EXCEEDED"])("keeps %s components visible and nonselectable", async reason => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...packet, inputs: [{ ...input, components: [{ ...component, eligible: false, ineligible_reason: reason }] }] });
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); await select(); expect(screen.getByRole("checkbox", { name: /기여 요약/ })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
});

it("enforces the 30 component limit without silently truncating or removing chosen items", async () => {
  const components = Array.from({ length: 31 }, (_, index) => ({ ...component, component_id: `/items/${index}`, label: `항목 ${index + 1}` }));
  vi.mocked(api.fetchJson).mockResolvedValue({ ...packet, inputs: [{ ...input, components }] });
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); await select();
  const boxes = screen.getAllByRole("checkbox"); boxes.slice(0, 30).forEach(box => fireEvent.click(box));
  expect(boxes[30]).toHaveProperty("disabled", true); expect(boxes[0]).toHaveProperty("disabled", false); fireEvent.click(boxes[0]); expect(boxes[30]).toHaveProperty("disabled", false);
  expect(posts()).toHaveLength(0);
});

it("shows read failures separately from an empty input list and retains an explicit retry", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new api.ApiError("forbidden", 403, "")).mockResolvedValueOnce({ packet_id: "packet-one", inputs: [] });
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); fireEvent.click(screen.getByText(summary)); await screen.findByRole("alert");
  expect(screen.queryByText(/이 작업에 선택된 저장 분석이 없습니다/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "저장된 분석 항목 다시 확인" })); await screen.findByText(/이 작업에 선택된 저장 분석이 없습니다/);
});

it("rejects malformed input metadata rather than treating it as an empty successful list", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...packet, inputs: [{ ...input, components: [{ ...component, support: "VERIFIED" }] }] });
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); fireEvent.click(screen.getByText(summary)); await screen.findByRole("alert");
  expect(screen.queryByLabelText("후보에 사용할 저장 분석")).toBeNull(); expect(posts()).toHaveLength(0);
});

it("does not present a previously empty list as current after a refresh failure", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ packet_id: "packet-one", inputs: [] }).mockRejectedValueOnce(new Error("read failed"));
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); fireEvent.click(screen.getByText(summary)); await screen.findByText(/이 작업에 선택된 저장 분석이 없습니다/);
  fireEvent.click(screen.getByRole("button", { name: "저장된 분석 항목 다시 확인" })); await screen.findByRole("alert");
  expect(screen.queryByText(/이 작업에 선택된 저장 분석이 없습니다/)).toBeNull(); expect(screen.getByText(/현재 상태는 확인되지 않았습니다/)).toBeTruthy();
});

it("allows keyboard selection and explicit save without submitting on component selection", async () => {
  const user = userEvent.setup(); vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockResolvedValueOnce({ candidate: candidate(), created: true });
  render(<ReviewInputJournal root={root} onSaved={onSaved}/>); await open(); await user.selectOptions(screen.getByLabelText("후보에 사용할 저장 분석"), input.receipt_id);
  screen.getByRole("checkbox", { name: /기여 요약/ }).focus(); await user.keyboard(" "); expect(posts()).toHaveLength(0);
  screen.getByRole("button", { name: saveLabel }).focus(); await user.keyboard("{Enter}"); await waitFor(() => expect(onSaved).toHaveBeenCalledWith("candidate-one"));
});

it("ignores a late save response after topic change and releases the old scope lock", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ReviewInputJournal root={root} onSaved={onSaved} onLocked={onLocked}/>); await choose(); fireEvent.click(screen.getByRole("button", { name: saveLabel }));
  await waitFor(() => expect(posts()).toHaveLength(1)); view.rerender(<ReviewInputJournal root="/other/job" onSaved={onSaved} onLocked={onLocked}/>);
  await act(async () => finish({ candidate: candidate(), created: true })); expect(onSaved).not.toHaveBeenCalled(); expect(screen.queryByRole("checkbox")).toBeNull();
  expect(onLocked).toHaveBeenLastCalledWith(false);
});

it("does not repeat an in-flight save on rapid clicks or update after unmount", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(packet).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ReviewInputJournal root={root} onSaved={onSaved}/>); await choose(); const save = screen.getByRole("button", { name: saveLabel }); fireEvent.click(save); fireEvent.click(save);
  expect(posts()).toHaveLength(1); view.unmount(); await act(async () => finish({ candidate: candidate(), created: true })); expect(onSaved).not.toHaveBeenCalled();
});

it("does not apply an old topic's late input list to the new topic", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ReviewInputJournal root={root} onSaved={onSaved}/>); fireEvent.click(screen.getByText(summary)); await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(1));
  view.rerender(<ReviewInputJournal root="/other/job" onSaved={onSaved}/>); await act(async () => finish(packet));
  expect(screen.queryByLabelText("후보에 사용할 저장 분석")).toBeNull(); expect(posts()).toHaveLength(0);
});
