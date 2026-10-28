export const HOST = "org.xrml.paper_collector";
// Match only documented browser errors. Other text can contain local paths or
// input values, so never forward it as a diagnostic or infer a specific cause.
const nativeErrors = new Map([
  ["Specified native messaging host not found.", "native_host_not_found"],
  ["Access to the specified native messaging host is forbidden.", "native_host_forbidden"],
  ["Native host has exited.", "native_host_exited"],
  ["Failed to start native messaging host.", "native_host_start_failed"],
  ["Error when communicating with the native messaging host.", "native_host_communication_failed"],
]);
const errorText = error => typeof error === "string" ? error : typeof error?.message === "string" ? error.message : "";
export function nativeFailureCode(error) {
  return nativeErrors.get(errorText(error).trim()) ?? "native_host_unavailable";
}
const checkSavedState = "앱의 저장 작업과 브라우저 다운로드 기록을 먼저 확인하세요. 결과를 확인하기 전에는 새 다운로드를 시작하지 마세요.";
const errorMessages = new Map([
  ["native_host_not_found", "브라우저가 XRML 연결 도구를 확인하지 못했습니다. 앱에 저장된 작업은 그대로 두고 Whale용 연결 도구의 설치·등록·설정 파일을 확인하세요. 이 오류만으로 파일 누락이나 설정 해석 실패 등 실제 원인을 단정할 수 없습니다. 연결 코드 재발급만으로는 해결되지 않습니다."],
  ["native_host_forbidden", "현재 확장 앱의 XRML 연결 도구 접근이 거부되었습니다. Whale 확장 ID와 연결 도구에 등록된 허용 확장 설정을 확인하세요. 앱에 저장된 작업은 그대로 유지하세요. 연결 코드 재발급만으로는 해결되지 않습니다."],
  ["native_host_exited", `XRML 연결 도구가 응답 전에 종료되어 요청 결과를 확인하지 못했습니다. 종료 원인은 아직 확인되지 않았습니다. ${checkSavedState}`],
  ["native_host_start_failed", "브라우저가 XRML 연결 도구를 시작하지 못했습니다. 연결 도구의 설치 상태와 실행 권한을 확인하세요. 앱에 저장된 작업은 그대로 유지하세요. 연결 코드 재발급만으로는 해결되지 않습니다."],
  ["native_host_communication_failed", `XRML 연결 도구의 통신 응답을 읽지 못했습니다. 연결 도구의 메시지 형식 확인이 필요합니다. ${checkSavedState}`],
  ["native_host_unavailable", `XRML 연결 도구의 응답을 확인하지 못했습니다. 이 오류만으로 설치·권한·종료 중 어느 문제인지 알 수 없습니다. ${checkSavedState}`],
  ["collector_channel_unavailable", `확장 화면과 백그라운드 사이의 응답을 확인하지 못했습니다. 연결 도구 설치 실패로 단정할 수 없습니다. ${checkSavedState}`],
  ["connect_in_app_first", "앱에서 저장한 수집 작업의 연결 코드를 이 화면에 입력한 뒤 작업을 연결하세요."],
  ["invalid_connection", "연결 코드의 형식을 확인하지 못했습니다. 앱에서 복사한 작업 연결 코드를 확인하세요. 코드는 검색어나 오류 보고에 붙여 넣지 마세요."],
  ["connection_expired_or_denied", "앱이 이 작업의 연결 권한을 확인하지 못했습니다. 앱의 기존 작업과 권한 상태를 확인하고, 만료·철회된 연결이면 해당 작업의 새 코드를 발급하세요."],
  ["app_unavailable_resume_after_reopen", `XRML 앱에 연결하지 못했습니다. 앱을 연 뒤 기존 작업의 상태를 확인하세요. ${checkSavedState}`],
  ["download_dispatch_unknown_check_browser_history", `이전 다운로드 요청의 시작 여부를 확인하지 못했습니다. ${checkSavedState} 새 시도는 다운로드 기록을 확인한 뒤 명시적으로 선택해야 합니다.`],
]);
for (const code of ["refresh_or_resume_in_app_first", "state_changed_refresh_first", "job_mismatch"])
  errorMessages.set(code, `앱과 확장의 작업 상태를 다시 확인해야 합니다. ${checkSavedState}`);
for (const code of ["redirect_out_of_scope", "source_out_of_scope", "download_identity_mismatch", "file_or_scope_rejected", "download_not_confirmed_complete", "download_path_rejected", "download_file_rejected", "download_bytes_mismatch", "download_scope_rejected", "download_changed", "download_is_not_complete_pdf", "invalid_download"])
  errorMessages.set(code, `다운로드의 파일·완료 상태·논문 대응 검사를 통과하지 못했습니다. 본문 확보 성공으로 처리하지 않았습니다. ${checkSavedState}`);
export function collectorErrorCode(error) {
  const code = errorText(error);
  return errorMessages.has(code) ? code : "collector_failed";
}
export function collectorErrorMessage(error) {
  return errorMessages.get(collectorErrorCode(error)) ?? `요청 결과를 확인하지 못했습니다. ${checkSavedState}`;
}
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
