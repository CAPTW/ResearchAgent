import type { TopicSchedulerStatus, TopicSchedulesResponse } from "./api";

export type ScheduleStoreUiState = {
  degraded: boolean;
  readOnly: boolean;
  locked: boolean;
  warnings: string[];
  pid?: number | null;
  ageSeconds?: number | null;
  stale?: boolean;
  ownerRunning?: boolean | null;
};

const SCHEDULE_STORE_WARNING_PATTERN = /예약 작업 저장소|스케줄 저장소|schedule store|schedules\.lock/i;

export function isScheduleStoreWarning(warning: string) {
  return SCHEDULE_STORE_WARNING_PATTERN.test(warning);
}

export function scheduleStoreStateFromWarnings(warnings: string[] = []): ScheduleStoreUiState {
  const scheduleWarnings = warnings.filter(isScheduleStoreWarning);
  const degraded = scheduleWarnings.length > 0;
  return {
    degraded,
    readOnly: degraded,
    locked: degraded,
    warnings: scheduleWarnings,
  };
}

export function scheduleStoreStateFromResponse(response: TopicSchedulesResponse): ScheduleStoreUiState {
  const warningState = scheduleStoreStateFromWarnings(response.warnings ?? []);
  const readOnly = Boolean(response.read_only) || warningState.readOnly;
  const degraded = Boolean(response.degraded) || readOnly || warningState.degraded;
  const lock = degraded ? response.scheduler?.store_lock : undefined;
  return {
    degraded,
    readOnly,
    locked: Boolean(lock?.locked) || warningState.locked,
    warnings: [...warningState.warnings],
    pid: lock?.pid ?? null,
    ageSeconds: lock?.age_seconds ?? null,
    stale: lock?.stale,
    ownerRunning: lock?.owner_running ?? null,
  };
}

export function scheduleStoreStateFromScheduler(
  scheduler: TopicSchedulerStatus | null | undefined,
  fallback: { degraded?: boolean; readOnly?: boolean; warnings?: string[] } = {},
): ScheduleStoreUiState {
  const lock = scheduler?.store_lock;
  const locked = Boolean(lock?.locked);
  const backendReadOnly = Boolean(scheduler?.backend?.read_only);
  const warningState = scheduleStoreStateFromWarnings(fallback.warnings ?? []);
  const readOnly = Boolean(fallback.readOnly) || backendReadOnly || locked || warningState.readOnly;
  const degraded = Boolean(fallback.degraded) || readOnly || warningState.degraded;
  return {
    degraded,
    readOnly,
    locked,
    warnings: [...warningState.warnings],
    pid: lock?.pid ?? null,
    ageSeconds: lock?.age_seconds ?? null,
    stale: lock?.stale,
    ownerRunning: lock?.owner_running ?? null,
  };
}

export function scheduleStoreBlockedMessage() {
  return "스케줄 저장소가 일시적으로 잠겨 있습니다. 잠시 후 다시 시도하세요.";
}

export function scheduleStoreOwnerDetail(state: ScheduleStoreUiState) {
  const parts: string[] = [];
  if (state.pid !== undefined && state.pid !== null) {
    parts.push(`owner ${state.pid}`);
  }
  if (state.ageSeconds !== undefined && state.ageSeconds !== null) {
    parts.push(`age ${Math.round(Number(state.ageSeconds))}s`);
  }
  if (state.ownerRunning === true) {
    parts.push("owner running");
  }
  if (state.stale) {
    parts.push("stale");
  }
  return parts.join(" · ");
}
