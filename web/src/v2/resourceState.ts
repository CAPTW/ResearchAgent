export type ResourcePhase =
  | "idle"
  | "loading"
  | "ready"
  | "empty"
  | "unavailable"
  | "permission_denied"
  | "error"
  | "retrying";

export type ResourceState<T> = {
  phase: ResourcePhase;
  data: T | null;
  message: string | null;
  stale: boolean;
  retryCount: number;
};

export const initialResourceState: ResourceState<never> = {
  phase: "idle",
  data: null,
  message: null,
  stale: false,
  retryCount: 0,
};

export function startResource<T>(state: ResourceState<T>): ResourceState<T> {
  return {
    ...state,
    phase: state.data === null ? "loading" : "retrying",
    message: null,
    stale: state.data !== null,
  };
}

function resourceIsEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object" && value !== null && "items" in value) {
    const items = (value as { items?: unknown }).items;
    return Array.isArray(items) && items.length === 0;
  }
  return false;
}

export function resolveResource<T>(state: ResourceState<T>, data: T): ResourceState<T> {
  return {
    ...state,
    phase: resourceIsEmpty(data) ? "empty" : "ready",
    data,
    message: null,
    stale: false,
  };
}

export function unavailableResource<T>(state: ResourceState<T>, message: string): ResourceState<T> {
  return { ...state, phase: "unavailable", message, stale: state.data !== null };
}

export function deniedResource<T>(state: ResourceState<T>, message: string): ResourceState<T> {
  return { ...state, phase: "permission_denied", message, stale: state.data !== null };
}

export function failResource<T>(state: ResourceState<T>, error: unknown): ResourceState<T> {
  return {
    ...state,
    phase: "error",
    message: error instanceof Error ? error.message : "리소스를 불러오지 못했습니다.",
    stale: state.data !== null,
  };
}

export function retryResource<T>(state: ResourceState<T>): ResourceState<T> {
  return {
    ...state,
    phase: "retrying",
    message: state.message,
    stale: state.data !== null,
    retryCount: state.retryCount + 1,
  };
}
