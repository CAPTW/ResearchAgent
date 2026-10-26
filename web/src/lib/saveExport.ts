/** Preserve server-provided text bytes. Native destinations come only from the OS picker. */
export async function saveExport(content: string, filename: string, mediaType: string): Promise<void> {
  if (window.__XRML_NATIVE_DOWNLOADS__ === 1) {
    const notify = (state: string, path: string | null = null, reason?: string) =>
      window.dispatchEvent(new CustomEvent("xrml:download-status", { detail: { state, filename, path, ...(reason ? { reason } : {}) } }));
    notify("choosing");
    try {
      const invoke = window.__TAURI__?.core?.invoke;
      if (!invoke) throw new Error("native_export_unavailable");
      const result = await invoke<unknown>("save_text_export", { filename, content });
      if (!result || typeof result !== "object") throw new Error("native_export_invalid_result");
      const value = result as Record<string, unknown>;
      if (value.state === "cancelled" && value.path === null && value.bytes === 0) {
        notify("cancelled");
        return;
      }
      if (value.state !== "saved" || typeof value.path !== "string" || !value.path
        || value.bytes !== new TextEncoder().encode(content).byteLength) throw new Error("native_export_invalid_result");
      notify("saved", value.path);
    } catch (error) {
      const busy = String(error).includes("export_busy");
      const reasons: Record<string, string> = {
        native_export_unavailable: "실행본에 저장 연결이 없습니다. 최신 실행본으로 다시 여세요.",
        native_export_invalid_result: "저장 결과를 확인할 수 없습니다. 대상 파일을 먼저 확인하세요.",
        export_not_allowed: "현재 화면에서 저장할 수 없는 파일 형식 또는 크기입니다.",
        export_window_unavailable: "저장할 앱 창을 찾지 못했습니다. 앱을 다시 여세요.",
        export_dialog_failed: "Windows 저장 창을 열지 못했습니다. 앱을 다시 열고 시도하세요.",
        export_write_failed: "선택한 위치에 쓸 수 없습니다. 다른 위치 또는 파일명을 선택하세요.",
        export_task_failed: "저장 작업이 중단되었습니다. 대상 파일을 확인하고 다시 시도하세요.",
      };
      const key = Object.keys(reasons).find(code => String(error).includes(code));
      const reason = busy ? "다른 파일 저장이 진행 중입니다. 완료 후 다시 시도하세요."
        : key ? reasons[key] : "네이티브 저장 연결이 거부되었습니다. 최신 실행본을 다시 여세요.";
      notify(busy ? "busy" : "failed", null, reason);
      throw new Error(`파일 저장을 완료하지 못했습니다. ${reason}`);
    }
    return;
  }
  const url = URL.createObjectURL(new Blob([content], { type: mediaType }));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename;
  document.body.appendChild(anchor); anchor.click(); anchor.remove();
  // The browser owns saving. Starting a download is not proof of a disk write.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
