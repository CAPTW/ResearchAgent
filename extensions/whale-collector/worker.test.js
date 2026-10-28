import { test } from "node:test";
import assert from "node:assert/strict";

let sequence = 0;
async function harness({ complete = true } = {}) {
  const session = {}, local = {}, calls = [], downloads = new Map();
  const connection = { base_url: "http://127.0.0.1:8765", job_id: "bc_fixture", token: "x".repeat(43) };
  const item = { item_id: "bci_fixture", candidate: { title: "Synthetic paper", pdf_url: "https://arxiv.org/pdf/2601.00001v1" }, state: "queued", active_attempt_id: null };
  const job = { job_id: connection.job_id, items: [item] };
  let listener, changed, nativeFailure;
  const storage = target => ({ async get(key) { return { [key]: structuredClone(target[key]) }; },
    async set(value) { Object.assign(target, structuredClone(value)); }, async remove(key) { delete target[key]; } });
  globalThis.whale = {
    storage: { local: storage(local), session: storage(session) },
    runtime: { id: "a".repeat(32), getURL: file => `chrome-extension://${"a".repeat(32)}/${file}`,
      onMessage: { addListener(value) { listener = value; } },
      async sendNativeMessage(_name, message) {
        calls.push(structuredClone(message));
        if (nativeFailure) throw nativeFailure;
        if (message.command === "transition") {
          if (message.payload.event === "start") { item.state = "downloading"; item.active_attempt_id = message.payload.attempt_id; }
          else item.state = message.payload.event;
        }
        if (message.command === "upload") item.state = "awaiting_confirmation";
        return { ok: true, data: structuredClone(job) };
      } },
    downloads: { onChanged: { addListener(value) { changed = value; } },
      async search({ id }) { return downloads.has(id) ? [downloads.get(id)] : []; },
      async download(options) {
        assert.equal(item.state, "downloading", "reserve must precede browser dispatch");
        assert.equal(local.attempts["bc_fixture:bci_fixture"].download_id, null);
        assert.match(options.filename, /^XRML\/bc_fixture\/bci_fixture\/[-\w]+\.pdf$/);
        downloads.set(17, { id: 17, byExtensionId: "a".repeat(32), url: options.url, finalUrl: options.url,
          filename: `/Downloads/${options.filename}`, state: complete ? "complete" : "in_progress", danger: "safe", exists: true,
          bytesReceived: 30, totalBytes: 30, fileSize: 30 });
        return 17;
      } },
  };
  await import(`./worker.js?test=${++sequence}`);
  const send = message => new Promise(resolve => {
    listener(message, { id: "a".repeat(32), url: globalThis.whale.runtime.getURL("popup.html") }, resolve);
  });
  assert.equal((await send({ command: "connect", ticket: JSON.stringify(connection) })).ok, true);
  return { send, calls, item, job, session, local, changed, downloads,
    failNative(error) { nativeFailure = error; } };
}

test("complete owned download registers ID before bounded native upload and awaits app confirmation", async () => {
  const h = await harness();
  const result = await h.send({ command: "download", item_id: "bci_fixture" });
  assert.equal(result.ok, true);
  assert.equal(h.item.state, "awaiting_confirmation");
  const upload = h.calls.findIndex(call => call.command === "upload");
  assert.ok(upload > h.calls.findIndex(call => call.command === "transition" && call.payload.download_id === 17));
  assert.equal(h.calls.filter(call => call.command === "upload").length, 1);
  assert.equal(h.local.attempts["bc_fixture:bci_fixture"].download_id, 17);
  assert.equal(JSON.stringify(h.local).includes(h.session.connection.token), false, "capability must not survive browser restart in local storage");
});

test("reconciliation after service worker restart finishes the persisted download without another download", async () => {
  const h = await harness({ complete: false });
  await h.send({ command: "download", item_id: "bci_fixture" });
  assert.equal(h.item.state, "downloading");
  h.downloads.get(17).state = "complete";
  assert.equal((await h.send({ command: "reconcile" })).ok, true);
  assert.equal(h.item.state, "awaiting_confirmation");
  assert.equal(h.downloads.size, 1);
});

test("a redirect to a login page never becomes an uploaded PDF", async () => {
  const h = await harness({ complete: false });
  await h.send({ command: "download", item_id: "bci_fixture" });
  h.downloads.get(17).state = "complete";
  h.downloads.get(17).finalUrl = "https://arxiv.org/login";
  const result = await h.send({ command: "reconcile" });
  assert.equal(result.ok, false);
  assert.equal(h.item.state, "auth_required");
  assert.equal(h.calls.some(call => call.command === "upload"), false);
});

test("unknown dispatch remains blocked until explicit browser-history confirmation", async () => {
  const h = await harness();
  h.local.attempts = { "bc_fixture:bci_fixture": { job_id: "bc_fixture", item_id: "bci_fixture", attempt_id: "old", download_id: null } };
  const result = await h.send({ command: "download", item_id: "bci_fixture" });
  assert.equal(result.error, "download_dispatch_unknown_check_browser_history");
  assert.equal(h.downloads.size, 0);
  assert.equal((await h.send({ command: "download", item_id: "bci_fixture", confirm_unknown: true })).ok, true);
});

test("native transport errors preserve saved attempts and connection without dispatching another download", async () => {
  const failures = [
    ["Specified native messaging host not found.", "native_host_not_found"],
    ["Access to the specified native messaging host is forbidden.", "native_host_forbidden"],
    ["Native host has exited.", "native_host_exited"],
    ["Failed to start native messaging host.", "native_host_start_failed"],
    ["Error when communicating with the native messaging host.", "native_host_communication_failed"],
    ["unexpected synthetic-secret-token", "native_host_unavailable"],
  ];
  for (const [raw, expected] of failures) {
    const h = await harness();
    h.local.attempts = { "bc_fixture:bci_fixture": { job_id: "bc_fixture", item_id: "bci_fixture", attempt_id: "old", download_id: null } };
    const before = structuredClone({ session: h.session, local: h.local, job: h.job });
    h.failNative(new Error(raw));
    const callsBefore = h.calls.length;
    assert.deepEqual(await h.send({ command: "download", item_id: "bci_fixture" }), { ok: false, error: expected });
    assert.deepEqual({ session: h.session, local: h.local, job: h.job }, before);
    assert.equal(h.calls.length, callsBefore + 1, "failed inspection must not retry the native request");
    assert.equal(h.downloads.size, 0);
    h.failNative(null);
    assert.equal((await h.send({ command: "download", item_id: "bci_fixture" })).error, "download_dispatch_unknown_check_browser_history");
    assert.equal(h.downloads.size, 0, "restored connection must still require unknown-outcome confirmation");
  }
});

test("a host exit during reconciliation leaves the persisted download available for later inspection", async () => {
  const h = await harness({ complete: false });
  await h.send({ command: "download", item_id: "bci_fixture" });
  const before = structuredClone({ session: h.session, local: h.local, job: h.job });
  h.failNative(new Error("Native host has exited."));
  assert.deepEqual(await h.send({ command: "reconcile" }), { ok: false, error: "native_host_exited" });
  assert.deepEqual({ session: h.session, local: h.local, job: h.job }, before);
  assert.equal(h.downloads.size, 1);
  h.failNative(null);
  h.downloads.get(17).state = "complete";
  assert.equal((await h.send({ command: "reconcile" })).ok, true);
  assert.equal(h.item.state, "awaiting_confirmation");
  assert.equal(h.downloads.size, 1, "reconciliation must reuse the saved download ID");
});

test("invalid connection input is not echoed by the worker and does not replace an existing connection", async () => {
  const h = await harness();
  const before = structuredClone(h.session);
  const result = await h.send({ command: "connect", ticket: 'synthetic-secret-token {"token":' });
  assert.deepEqual(result, { ok: false, error: "collector_failed" });
  assert.deepEqual(h.session, before);
  assert.equal(h.downloads.size, 0);
});
