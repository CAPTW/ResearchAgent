import type { SettingsStatusResponse, TopicSchedulerStatus } from "./api";

type QueueCounts = NonNullable<TopicSchedulerStatus["queue_item_counts"]>;

export function schedulerQueueCounts(status?: TopicSchedulerStatus | null): QueueCounts | null {
  if (status?.queue_item_counts) {
    return status.queue_item_counts;
  }
  const queue = status?.backend?.queue as QueueCounts | undefined;
  return queue ?? null;
}

export function queueBackendLabel(status?: TopicSchedulerStatus | null): string {
  if (!status) {
    return "큐 설정 필요";
  }
  if (status.runtime_mode === "legacy_file_read_only" || status.backend?.read_only) {
    return "큐 읽기 전용";
  }
  if (status.queue_backend_status === "available") {
    return "큐 사용 가능";
  }
  if (status.queue_backend_status === "unavailable") {
    return "큐 실패 / degraded";
  }
  if (status.configured_runtime_mode === "queue_backed" && status.configured_runtime_mode_valid === false) {
    return "큐 설정 필요";
  }
  if (status.configured_runtime_mode === "queue_backed" && status.runtime_mode !== "queue_backed") {
    return "큐 설정 필요";
  }
  return "큐 비활성화";
}

export function distributedSupportLabel(status?: TopicSchedulerStatus | null, settings?: SettingsStatusResponse | null): string {
  if (status?.multi_node_dispatch_safe || settings?.deployment?.distributed_scheduler_supported) {
    return "분산 지원됨";
  }
  if (status?.distributed_support_status === "unsupported" || settings?.deployment?.scheduler_distributed_support_status === "unsupported") {
    return "분산 지원 안 함";
  }
  return "분산 준비 중";
}

export function queueCountLabel(counts?: QueueCounts | null): string | null {
  if (!counts) {
    return null;
  }
  return `queue q${counts.queued ?? 0}/l${counts.leased ?? 0}/x${counts.expired ?? 0}/f${counts.failed ?? 0}`;
}

export function compactQueueCountLabel(counts?: QueueCounts | null): string | null {
  if (!counts) {
    return null;
  }
  return `q${counts.queued ?? 0}/l${counts.leased ?? 0}/x${counts.expired ?? 0}/f${counts.failed ?? 0}`;
}
