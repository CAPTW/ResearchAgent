export const HOST = "org.xrml.paper_collector";
const identity = value => typeof value === "string" && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/.test(value);
export function parseConnection(text) {
  const value = JSON.parse(text);
  if (!value || Object.keys(value).sort().join() !== "base_url,job_id,token") throw Error("invalid_connection");
  const url = new URL(value.base_url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.username || url.password
      || url.pathname !== "/" || url.search || url.hash || !identity(value.job_id)
      || typeof value.token !== "string" || !/^[a-zA-Z0-9_-]{32,128}$/.test(value.token)) throw Error("invalid_connection");
  return { ...value, base_url: url.origin };
}
export function downloadPath(job, item, attempt) {
  if (![job, item, attempt].every(identity)) throw Error("invalid_identity");
  return `XRML/${job}/${item}/${attempt}.pdf`;
}
export function scopedPdf(url, expected) {
  if (url !== expected || !/^https:\/\/arxiv\.org\/pdf\/\d{4}\.\d{4,5}v[1-9]\d{0,3}$/.test(url)) throw Error("source_out_of_scope");
  return url;
}
export function downloadObservation(item, binding, extensionId) {
  if (item.id !== binding.download_id || item.byExtensionId !== extensionId) throw Error("download_identity_mismatch");
  scopedPdf(item.url, binding.pdf_url);
  // Cross-host/version redirects require a new reviewed adapter, never implicit trust.
  if (item.finalUrl && item.finalUrl !== binding.pdf_url) throw Error("redirect_out_of_scope");
  const keys = ["id", "byExtensionId", "url", "finalUrl", "filename", "state", "exists", "danger", "mime", "bytesReceived", "totalBytes", "fileSize"];
  return Object.fromEntries(keys.filter(key => item[key] !== undefined).map(key => [key, item[key]]));
}
