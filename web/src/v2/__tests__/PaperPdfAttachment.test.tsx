import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { PaperPdfAttachment } from "../components/PaperPdfAttachment";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const digest = "a".repeat(64), target = { target_id: "pdft_" + "b".repeat(64), run_id: "run" };
const attachment = { schema_version: "PAPER_PDF_ATTACHMENT_V1", attachment_id: "pdfa_" + "c".repeat(64), topic_id: "topic", paper_version_id: "version", target_id: target.target_id, run_id: "run", paper_id: "paper", pdf_sha256: digest, pdf_bytes: 8, artifact_ref: `run:attachments/pdf/${digest}.pdf`, created_at: "2026-10-18T09:00:00Z", created_by: "owner", binding: "USER_SELECTED_NOT_IDENTITY_VERIFIED", state: "ATTACHED_NOT_EXTRACTED_OR_REVIEWED" };
const inventory = { schema_version: "PAPER_PDF_ATTACHMENTS_V1", topic_id: "topic", paper_version_id: "version", targets: [target], attachments: [], max_pdf_bytes: 25000000 };
const onAttached = vi.fn(), root = "/api/topics/topic/paper-bank/version/review-preparations";
const props = { root, topicId: "topic", paperVersionId: "version", disabled: false, onAttached };
const file = () => new File(["%PDF-1.7"], "논문.pdf", { type: "application/pdf" });
const writes = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "PUT");
async function open() { fireEvent.click(screen.getByText("내 PDF 파일 연결")); await screen.findByLabelText("연결할 PDF 파일"); }
async function choose() {
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("PDF를 연결할 출처 관측"), target.target_id);
  const selected = file(); await user.upload(screen.getByLabelText("연결할 PDF 파일"), selected);
  return selected;
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("crypto", { subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(170).buffer) } });
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "PUT" ? { attachment, created: true } : inventory) as never);
});
afterEach(() => vi.unstubAllGlobals());

it("keeps file selection local, requires confirmation, and permits cancellation without upload", async () => {
  render(<PaperPdfAttachment {...props}/>); expect(api.fetchJson).not.toHaveBeenCalled(); await open(); await choose();
  expect(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" })).toHaveProperty("disabled", true); expect(writes()).toHaveLength(0);
  await userEvent.setup().click(screen.getByRole("button", { name: "PDF 파일 선택 취소" }));
  expect(screen.getByLabelText("연결할 PDF 파일")).toHaveProperty("value", ""); expect(writes()).toHaveLength(0);
});

it("sends only exact PDF bytes through keyboard confirmation and leaves extraction to a separate action", async () => {
  render(<PaperPdfAttachment {...props}/>); await open(); const selected = await choose(); const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox")); const button = screen.getByRole("button", { name: "선택한 PDF 복사본 연결" }); button.focus(); await user.keyboard("{Enter}");
  await screen.findByText("PDF 복사본을 연결했습니다. 추출할 PDF를 직접 선택하세요.");
  expect(writes()).toHaveLength(1); expect(writes()[0]).toEqual([`${root}/pdf-attachments/${target.target_id}/${digest}`, { method: "PUT", headers: { "Content-Type": "application/pdf" }, body: selected }]);
  expect(onAttached).toHaveBeenCalledTimes(1); expect(screen.getByLabelText("연결할 PDF 파일")).toHaveProperty("value", "");
  expect(vi.mocked(api.fetchJson).mock.calls.some(([path]) => path.includes("pdf-extractions"))).toBe(false);
});

it("freezes File, hash and target after lost response and explicitly replays once", async () => {
  let calls = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => { if (init?.method === "PUT") { if (++calls === 1) throw new TypeError("lost response"); return { attachment, created: false } as never; } return inventory as never; });
  render(<PaperPdfAttachment {...props}/>); await open(); const selected = await choose();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" }));
  const retry = await screen.findByRole("button", { name: "같은 PDF 연결 요청 다시 확인" });
  expect(screen.getByLabelText("연결할 PDF 파일")).toHaveProperty("disabled", true); expect(screen.getByLabelText("PDF를 연결할 출처 관측")).toHaveProperty("disabled", true); expect(writes()).toHaveLength(1);
  fireEvent.click(retry); await screen.findByText("같은 PDF의 첨부 이력을 확인했습니다. 중복 저장하지 않았습니다.");
  expect(writes()).toHaveLength(2); expect(writes()[0]).toEqual(writes()[1]); expect(writes()[1][1]?.body).toBe(selected);
  expect(crypto.subtle.digest).toHaveBeenCalledTimes(1);
});

it("keeps file after a definite rejection and requires a fresh listing before retry", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => { if (init?.method === "PUT") throw new api.ApiError("review_pdf_encrypted", 422, ""); return inventory as never; });
  render(<PaperPdfAttachment {...props}/>); await open(); await choose(); fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" }));
  await screen.findByText("암호로 잠긴 PDF는 반입할 수 없습니다."); expect(screen.getByText(/선택: 논문.pdf/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "같은 PDF 연결 요청 다시 확인" })).toBeNull();
  expect(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "PDF 첨부 이력 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" })).toHaveProperty("disabled", false));
});

it("refuses wrong-scope acknowledgements and retains exact pending command", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "PUT" ? { attachment: { ...attachment, topic_id: "other" }, created: true } : inventory) as never);
  render(<PaperPdfAttachment {...props}/>); await open(); await choose(); fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" }));
  await screen.findByRole("button", { name: "같은 PDF 연결 요청 다시 확인" }); expect(onAttached).not.toHaveBeenCalled(); expect(screen.getByRole("alert").textContent).toContain("응답을 확인할 수 없습니다");
});

it("ignores a late acknowledgement after topic switch", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "PUT" ? new Promise(r => { resolve = r; }) as never : inventory as never);
  const view = render(<PaperPdfAttachment {...props}/>); await open(); await choose(); fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" }));
  await waitFor(() => expect(writes()).toHaveLength(1)); view.rerender(<PaperPdfAttachment {...props} topicId="other" root="/other"/>);
  await act(async () => resolve({ attachment, created: true })); expect(onAttached).not.toHaveBeenCalled(); expect(screen.queryByText("PDF 복사본을 연결했습니다. 추출할 PDF를 직접 선택하세요.")).toBeNull();
});

it("rejects an empty or oversized selection before hashing or sending", async () => {
  render(<PaperPdfAttachment {...props}/>); await open();
  for (const selected of [new File([], "empty.pdf", { type: "application/pdf" }), Object.defineProperty(file(), "size", { value: 25000001 })]) {
    fireEvent.change(screen.getByLabelText("연결할 PDF 파일"), { target: { files: [selected] } });
    await screen.findByRole("alert");
  }
  expect(writes()).toHaveLength(0); expect(crypto.subtle.digest).not.toHaveBeenCalled();
});

it("keeps authorized historical attachments visible without offering their retired target for a new upload", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...inventory, targets: [], attachments: [attachment] });
  render(<PaperPdfAttachment {...props}/>); await open();
  expect(await screen.findByText(`첨부 ID: ${attachment.attachment_id}`)).toBeTruthy();
  expect(screen.getByRole("button", { name: "선택한 PDF 복사본 연결" })).toHaveProperty("disabled", true);
});
