import { test } from "node:test";
import assert from "node:assert/strict";
import { parseConnection, downloadPath, downloadObservation } from "./protocol.js";

test("ticket only targets exact numeric loopback with an opaque job capability", () => {
  const ticket = { base_url: "http://127.0.0.1:8765", job_id: "bc_fixture", token: "x".repeat(43) };
  assert.deepEqual(parseConnection(JSON.stringify(ticket)), ticket);
  for (const base_url of ["http://example.org:8765", "http://127.0.0.1:8765/path", "http://user@127.0.0.1:8765", "http://localhost:8765"]) {
    assert.throws(() => parseConnection(JSON.stringify({ ...ticket, base_url })));
  }
});
test("download filename is generated from bounded identities", () => {
  assert.equal(downloadPath("bc_fixture", "bci_fixture", "attempt"), "XRML/bc_fixture/bci_fixture/attempt.pdf");
  for (const item of ["../secret", "a:b", "a/b", ""]) assert.throws(() => downloadPath("job", item, "attempt"));
});
test("receipt binds extension, download ID and exact paper version", () => {
  const binding = { download_id: 7, pdf_url: "https://arxiv.org/pdf/2601.00001v1" };
  const item = { id: 7, byExtensionId: "extension", url: binding.pdf_url, finalUrl: binding.pdf_url, state: "complete" };
  assert.equal(downloadObservation(item, binding, "extension").id, 7);
  for (const invalid of [{ ...item, id: 8 }, { ...item, byExtensionId: "other" }, { ...item, finalUrl: "https://arxiv.org/login" }, { ...item, url: "https://arxiv.org/pdf/2601.00001v2" }]) {
    assert.throws(() => downloadObservation(invalid, binding, "extension"));
  }
});
