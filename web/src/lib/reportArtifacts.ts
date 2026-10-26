import type { ApiReportHubItem, ExportArtifact } from "./api";

export function exportArtifactFormatLabel(format?: string | null) {
  if (format === "markdown" || format === "report_markdown") return "Markdown";
  if (format === "pdf" || format === "report_pdf") return "PDF";
  if (format === "journal_pdf") return "저널 PDF";
  return format || "형식 미확인";
}

export function exportArtifactStatusLabel(status?: string | null) {
  if (status === "completed" || status === "ready") return "완료";
  if (status === "failed") return "실패";
  if (status === "unavailable") return "사용 불가";
  return "상태 미확인";
}

export function exportArtifactBadgeLabel(artifact: Pick<ExportArtifact, "format" | "status">) {
  return `${exportArtifactFormatLabel(artifact.format)} ${exportArtifactStatusLabel(artifact.status)}`;
}

export function reportDownloadableArtifactCount(report: Pick<ApiReportHubItem, "exportArtifacts" | "exportArtifactSummary">) {
  if (typeof report.exportArtifactSummary?.downloadable === "number") {
    return report.exportArtifactSummary.downloadable;
  }
  return (report.exportArtifacts ?? []).filter((artifact) => artifact.download_available !== false && ["completed", "ready"].includes(artifact.status)).length;
}

