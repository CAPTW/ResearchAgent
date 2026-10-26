import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import { captureSnapshot, getDiff, listSnapshots, type Snapshot, type SnapshotDiff, type SnapshotList } from "../lib/temporalMemory";

export type MemoryResource<T> = { phase: "loading" | "ready" | "error" | "unavailable"; data: T | null; message?: string };
const loading = <T,>(): MemoryResource<T> => ({ phase: "loading", data: null });
function failed<T>(error: unknown): MemoryResource<T> {
  return { phase: error instanceof ApiError && [409, 503].includes(error.status) ? "unavailable" : "error", data: null,
    message: error instanceof Error ? error.message : "리소스를 불러오지 못했습니다." };
}
export function useTemporalMemory(topicId: string) {
  const [list, setList] = useState<MemoryResource<SnapshotList>>(loading);
  const [selected, setSelected] = useState<Snapshot | null>(null);
  const [delta, setDelta] = useState<MemoryResource<SnapshotDiff>>(loading);
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const generation = useRef(0);
  const topicGeneration = useRef(0);
  const captureLock = useRef(false);
  const selectedRef = useRef(selected); selectedRef.current = selected;
  const activeTopic = useRef(topicId); activeTopic.current = topicId;

  const load = useCallback(async (targetPage: number, preferred?: Snapshot) => {
    const current = ++generation.current;
    setList(loading());
    try {
      const result = await listSnapshots(topicId, targetPage);
      if (current !== generation.current || activeTopic.current !== topicId) return;
      setList({ phase: "ready", data: result });
      // A repeated capture can return an older immutable record outside this page.
      setSelected(preferred ?? result.items[0] ?? null);
    } catch (error) {
      if (current === generation.current && activeTopic.current === topicId) setList(failed(error));
    }
  }, [topicId]);
  useEffect(() => {
    topicGeneration.current++;
    setSelected(null); setPage(1); setMessage(""); setCaptureError(null); setBusy(false); captureLock.current = false;
    void load(1);
    return () => { generation.current++; topicGeneration.current++; };
  }, [load]);
  useEffect(() => {
    let cancelled = false;
    setDelta(loading());
    if (selected?.parent_snapshot_id) {
      void getDiff(topicId, selected.parent_snapshot_id, selected.snapshot_id).then(
        data => { if (!cancelled) setDelta({ phase: "ready", data }); },
        error => { if (!cancelled) setDelta(failed(error)); },
      );
    }
    return () => { cancelled = true; };
  }, [topicId, selected, revision]);

  const refresh = useCallback(async () => {
    setRevision(value => value + 1);
    await load(page, selectedRef.current ?? undefined);
  }, [load, page]);
  const changePage = (value: number) => { setPage(value); void load(value); };
  const select = (id: string) => {
    const snapshot = list.data?.items.find(item => item.snapshot_id === id);
    if (snapshot) setSelected(snapshot);
  };
  const capture = async () => {
    if (captureLock.current) return;
    captureLock.current = true; setBusy(true); setCaptureError(null); setMessage("");
    const current = topicGeneration.current;
    try {
      const result = await captureSnapshot(topicId);
      if (activeTopic.current !== topicId || current !== topicGeneration.current) return;
      setMessage(result.created ? "현재 상태 스냅샷을 만들었습니다." : "동일한 상태가 있어 기존 스냅샷을 반환했습니다.");
      setPage(1);
      await load(1, result.snapshot);
    } catch (error) { if (current === topicGeneration.current) setCaptureError(error instanceof Error ? error.message : "스냅샷 생성 실패"); }
    finally { if (current === topicGeneration.current) { captureLock.current = false; setBusy(false); } }
  };
  const current = list.phase === "ready" ? selected : null;
  const currentDelta = delta.data?.to_snapshot.snapshot_id === current?.snapshot_id ? delta : { ...delta, data: null };
  return { list, selected: current, delta: currentDelta, page, changePage, select, busy, captureError, message, capture, refresh };
}
