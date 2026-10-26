export function buildRunWorkbenchPath(runId: string, topicId?: string | null, suffix = "") {
  const encodedRunId = encodeURIComponent(runId);
  const normalizedSuffix = suffix && !suffix.startsWith("?") && !suffix.startsWith("#") ? `/${suffix}` : suffix;
  if (topicId) {
    return `/topics/${encodeURIComponent(topicId)}/runs/${encodedRunId}${normalizedSuffix}`;
  }
  return `/runs/${encodedRunId}${normalizedSuffix}`;
}
