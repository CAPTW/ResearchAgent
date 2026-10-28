import { collectorErrorMessage } from "./protocol.js";

const api = globalThis.whale ?? globalThis.chrome;
const status = document.querySelector("#status");
const items = document.querySelector("#items");
const labels = { queued: "다운로드 대기", downloading: "다운로드 중", auth_required: "브라우저 로그인 필요", interrupted: "중단됨 · 앱에서 재개", awaiting_confirmation: "앱에서 PDF 대응 확인", saved: "Papers에 저장됨", failed: "검사 실패" };
let busy = false;
async function send(command, extra = {}) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    // A lost popup/worker response is not evidence that the native host is absent.
    const response = await api.runtime.sendMessage({ command, ...extra }).catch(() => { throw Error("collector_channel_unavailable"); });
    if (!response?.ok) throw Error(response?.error ?? "collector_channel_unavailable");
    const job = response.data?.job ?? (response.data?.job_id ? response.data : null);
    if (job) render(job);
    status.textContent = response.data?.notice ?? (job ? "상태를 확인했습니다." : "연결을 해제했습니다. 앱에서도 권한을 철회할 수 있습니다.");
    if (command === "connect") document.querySelector("#ticket").value = "";
    if (command === "disconnect") items.replaceChildren();
  } catch (error) {
    status.textContent = collectorErrorMessage(error);
    if (error.message === "download_dispatch_unknown_check_browser_history" && extra.item_id) {
      const retry = document.createElement("button");
      retry.textContent = "다운로드 기록을 확인했습니다 · 새 시도 시작";
      retry.addEventListener("click", () => void send("download", { item_id: extra.item_id, confirm_unknown: true }));
      status.append(document.createElement("br"), retry);
    }
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach(button => { button.disabled = button.dataset.unavailable === "true"; });
  }
}
function render(job) {
  items.replaceChildren();
  for (const item of job.items) {
    const row = document.createElement("li");
    const title = document.createElement("span"); title.textContent = item.candidate.title;
    const state = document.createElement("p"); state.textContent = labels[item.state] ?? "상태 미확인";
    const button = document.createElement("button"); button.textContent = "이 논문 다운로드";
    button.dataset.unavailable = String(item.state !== "queued"); button.disabled = item.state !== "queued";
    button.addEventListener("click", () => void send("download", { item_id: item.item_id }));
    const source = document.createElement("a"); source.textContent = "원문 페이지 열기";
    source.href = /^https:\/\/arxiv\.org\/abs\/\d{4}\.\d{4,5}v[1-9]\d*$/.test(item.candidate.source_url) ? item.candidate.source_url : "#";
    source.target = "_blank"; source.rel = "noopener noreferrer";
    row.append(title, state, source, button); items.append(row);
  }
}
document.querySelector("#connect").addEventListener("click", () => void send("connect", { ticket: document.querySelector("#ticket").value }));
document.querySelector("#disconnect").addEventListener("click", () => void send("disconnect"));
document.querySelector("#refresh").addEventListener("click", () => void send("reconcile"));
void send("inspect");
