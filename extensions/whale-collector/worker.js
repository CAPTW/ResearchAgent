import { HOST, parseConnection, downloadPath, scopedPdf, downloadObservation, nativeFailureCode, collectorErrorCode, collectorErrorMessage } from "./protocol.js";

const api = globalThis.whale ?? globalThis.chrome;
// Serialize commands/events so a completion never overtakes its persisted ID.
let pending = Promise.resolve();
function serial(operation) {
  const next = pending.then(operation, operation);
  pending = next.catch(() => {});
  return next;
}
async function native(command, connection, data = {}) {
  const response = await api.runtime.sendNativeMessage(HOST, { command, connection, ...data })
    .catch(error => { throw Error(nativeFailureCode(error)); });
  if (!response?.ok) throw Error(response?.error ?? "native_host_unavailable");
  return response.data;
}
async function current() {
  const { connection } = await api.storage.session.get("connection");
  if (!connection) throw Error("connect_in_app_first");
  return parseConnection(JSON.stringify(connection));
}
async function records() { return (await api.storage.local.get("attempts")).attempts ?? {}; }
async function persist(attempts) { await api.storage.local.set({ attempts }); }
async function notice(text) { await api.storage.session.set({ notice: text }); }
async function reconcile(binding, connection) {
  if (binding.job_id !== connection.job_id || !Number.isInteger(binding.download_id)) return;
  const job = await native("inspect", connection);
  const item = job.items.find(value => value.item_id === binding.item_id);
  if (!item || item.active_attempt_id !== binding.attempt_id || item.state !== "downloading") return;
  const [download] = await api.downloads.search({ id: binding.download_id });
  if (!download) {
    await native("transition", connection, { item_id: binding.item_id,
      payload: { event: "interrupted", attempt_id: binding.attempt_id, download_id: binding.download_id } });
    return;
  }
  let observed;
  try {
    observed = downloadObservation(download, binding, api.runtime.id);
  } catch (error) {
    if (["redirect_out_of_scope", "source_out_of_scope", "download_identity_mismatch"].includes(error.message)) {
      // Record the rejected observation; it is never counted as acquired text.
      const event = download.id === binding.download_id && download.byExtensionId === api.runtime.id
        && /^https:\/\/arxiv\.org\/(?:login|user\/login)(?:[/?#]|$)/.test(download.finalUrl ?? "") ? "auth_required" : "interrupted";
      await native("transition", connection, { item_id: binding.item_id,
        payload: { event, attempt_id: binding.attempt_id, download_id: binding.download_id } });
    }
    throw error;
  }
  // Re-register a persisted download if the app missed its acknowledgment.
  await native("transition", connection, { item_id: binding.item_id, payload: {
    event: "start", attempt_id: binding.attempt_id, download_id: binding.download_id, source_url: binding.pdf_url } });
  if (observed.state === "complete") {
    try {
      await native("upload", connection, { item_id: binding.item_id, attempt_id: binding.attempt_id, download: observed });
    } catch (error) {
      if (["download_not_confirmed_complete", "download_path_rejected", "download_file_rejected", "download_bytes_mismatch",
        "download_scope_rejected", "download_changed", "download_is_not_complete_pdf"].includes(error.message)) {
        await native("transition", connection, { item_id: binding.item_id,
          payload: { event: "interrupted", attempt_id: binding.attempt_id, download_id: binding.download_id } });
      }
      throw error;
    }
    await notice("PDF 검사가 완료되었습니다. 앱에서 파일과 논문을 확인한 뒤 저장하세요.");
  } else if (observed.state === "interrupted") {
    await native("transition", connection, { item_id: binding.item_id, payload: {
      event: download.error === "SERVER_UNAUTHORIZED" ? "auth_required" : "interrupted",
      attempt_id: binding.attempt_id, download_id: binding.download_id } });
    await notice("다운로드가 중단되었습니다. 로그인·MFA·CAPTCHA가 있으면 브라우저에서 직접 처리한 후 앱에서 재개하세요.");
  }
}
async function execute(message) {
  if (message.command === "connect") {
    const connection = parseConnection(message.ticket);
    const job = await native("inspect", connection);
    if (job.job_id !== connection.job_id) throw Error("job_mismatch");
    await api.storage.session.set({ connection, notice: "작업에 연결되었습니다. 각 논문을 선택해 다운로드하세요." });
    return job;
  }
  if (message.command === "disconnect") {
    await api.storage.session.remove("connection");
    return null;
  }
  const connection = await current();
  const job = await native("inspect", connection);
  if (message.command === "inspect") return { job, notice: (await api.storage.session.get("notice")).notice };
  if (message.command === "reconcile") {
    const attempts = await records();
    for (const item of job.items) {
      const binding = attempts[`${job.job_id}:${item.item_id}`];
      if (binding && item.state === "downloading") await reconcile(binding, connection);
    }
    return { job: await native("inspect", connection) };
  }
  if (message.command !== "download") throw Error("command_not_supported");
  const item = job.items.find(value => value.item_id === message.item_id);
  if (!item || item.state !== "queued") throw Error("refresh_or_resume_in_app_first");
  const attempts = await records();
  const key = `${job.job_id}:${item.item_id}`;
  const prior = attempts[key];
  if (prior && !Number.isInteger(prior.download_id) && !message.confirm_unknown) {
    // A browser crash after dispatch but before ID persistence is unknown. Do not
    // silently download twice; the operator must inspect browser download history.
    throw Error("download_dispatch_unknown_check_browser_history");
  }
  const attempt = crypto.randomUUID();
  const pdf = scopedPdf(item.candidate.pdf_url, item.candidate.pdf_url);
  const binding = { job_id: job.job_id, item_id: item.item_id, attempt_id: attempt, pdf_url: pdf, download_id: null };
  attempts[key] = binding;
  await persist(attempts);
  await native("transition", connection, { item_id: item.item_id,
    payload: { event: "start", attempt_id: attempt, source_url: pdf } });
  // Downloads API uses this browser's existing site session. No cookie permission
  // or cookie value is requested or sent to the app/native host.
  const id = await api.downloads.download({ url: pdf, filename: downloadPath(job.job_id, item.item_id, attempt),
    conflictAction: "uniquify", saveAs: false });
  binding.download_id = id;
  await persist(attempts);
  await reconcile(binding, connection);
  return { job: await native("inspect", connection) };
}
api.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== api.runtime.id || sender.url !== api.runtime.getURL("popup.html")) return false;
  serial(() => execute(message)).then(data => respond({ ok: true, data }), error => respond({ ok: false, error: collectorErrorCode(error) }));
  return true;
});
api.downloads.onChanged.addListener(delta => {
  if (!delta.state && !delta.exists) return;
  void serial(async () => {
    const connection = await current();
    const attempts = await records();
    const binding = Object.values(attempts).find(value => value.download_id === delta.id && value.job_id === connection.job_id);
    if (binding) await reconcile(binding, connection);
  }).catch(error => notice(collectorErrorMessage(error)));
});
