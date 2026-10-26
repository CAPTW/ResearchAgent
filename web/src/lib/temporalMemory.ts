import { fetchJson } from "./api";

export const eventTypes = ["PAPER_ADDED", "PAPER_REMOVED", "PAPER_VERSION_CHANGED", "COLLECTION_MEMBERSHIP_CHANGED", "REVIEW_STATE_CHANGED", "FULL_TEXT_STATE_CHANGED", "EVIDENCE_CHANGED", "RIGHTS_STATE_CHANGED"] as const;
export type EventType = typeof eventTypes[number];
export type Snapshot = {
  snapshot_id: string; topic_id: string; created_at: string; schema_version: number;
  corpus_digest: string; source_revision: string; review_state_digest: string; evidence_digest: string;
  paper_count: number; parent_snapshot_id: string | null; acceptance_state: string; algorithm_version: string;
};
export type SnapshotList = { items: Snapshot[]; total: number; page: number; pageSize: number };
export type ChangeEvent = {
  event_id: string; event_type: EventType; canonical_paper_id: string; paper_version_id: string;
  before_digest: string; after_digest: string; before_state: string; after_state: string;
  from_snapshot_id: string; to_snapshot_id: string; source_snapshot_id: string;
  source_entity_ids: string[]; source_evidence_ids: string[]; acceptance_state: string; algorithm_version: string;
  paper_destination: string | null; evidence_destinations: Array<{id: string; destination: string}>;
  destination_reason?: string | null; unavailable_evidence_ids?: string[];
};
export type SnapshotDiff = {
  from_snapshot: Snapshot; to_snapshot: Snapshot; events: ChangeEvent[]; counts: Record<EventType, number>;
  changed_paper_count: number; unchanged_paper_count: number; algorithm_version: string; calculated_at: string;
  membership_only_changed_paper_count: number;
};
const root = (topic: string) => `/api/topics/${encodeURIComponent(topic)}/memory`;
export const listSnapshots = (topic: string, page = 1) => fetchJson<SnapshotList>(`${root(topic)}/snapshots?pageSize=25&page=${page}`);
export const captureSnapshot = (topic: string) => fetchJson<{ snapshot: Snapshot; created: boolean }>(`${root(topic)}/snapshots`, { method: "POST" });
export const getDiff = (topic: string, from: string, to: string) => fetchJson<SnapshotDiff>(`${root(topic)}/diff?${new URLSearchParams({ from, to })}`);
