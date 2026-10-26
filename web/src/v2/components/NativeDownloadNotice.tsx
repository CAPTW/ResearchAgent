import { useEffect, useState } from "react";
import "./NativeDownloadNotice.css";

declare global {
  interface Window { __XRML_NATIVE_DOWNLOADS__?: number }
}
type Notice = { state: "choosing" | "saving" | "saved" | "cancelled" | "failed" | "busy"; filename: string; path: string | null; reason?: string };

function parse(value: unknown): Notice | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if (!["choosing", "saving", "saved", "cancelled", "failed", "busy"].includes(String(data.state))
    || typeof data.filename !== "string"
    || (data.reason !== undefined && typeof data.reason !== "string")
    || !(data.path === null || typeof data.path === "string")
    || (["saved", "saving"].includes(String(data.state)) && !data.path)) return null;
  return data as Notice;
}

export function NativeDownloadNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);
  useEffect(() => {
    if (window.__XRML_NATIVE_DOWNLOADS__ !== 1) return;
    const handle = (event: Event) => {
      const next = parse((event as CustomEvent<unknown>).detail);
      if (next) setNotice(next);
    };
    window.addEventListener("xrml:download-status", handle);
    return () => window.removeEventListener("xrml:download-status", handle);
  }, []);
  if (!notice) return null;
  const messages = {
    choosing: "저장 위치를 선택해 주세요. 저장 완료 안내가 나올 때까지 기다려 주세요.",
    saving: "파일 저장 중… 완료 안내가 나올 때까지 기다려 주세요.",
    saved: "파일을 저장했습니다.",
    cancelled: "저장을 취소했습니다. 파일을 저장하지 않았습니다.",
    failed: "파일 저장을 완료하지 못했습니다. 저장 위치를 확인한 뒤 다시 내보내세요.",
    busy: "다른 파일 저장이 진행 중입니다. 완료한 뒤 다시 내보내세요.",
  };
  return <aside className="native-download-notice" aria-label="파일 저장 안내">
    <div role={notice.state === "failed" || notice.state === "busy" ? "alert" : "status"} aria-atomic="true">
      <strong>{messages[notice.state]}</strong>
      {notice.reason ? <p>{notice.reason}</p> : null}
      <p>{notice.filename}</p>
      {notice.path ? <p className="native-download-path">{notice.path}</p> : null}
    </div>
    <button type="button" onClick={() => setNotice(null)} aria-label="파일 저장 안내 닫기">닫기</button>
  </aside>;
}
