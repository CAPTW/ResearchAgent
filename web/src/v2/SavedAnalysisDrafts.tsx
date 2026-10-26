import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { AnalysisCommand, AnalysisState } from "./savedAnalyses";

export type AnalysisDraft = { note: string; state: AnalysisState; base: string | null; baseline: string; busy: boolean; attempt: AnalysisCommand | null; error: string; message: string; conflict: boolean };
export const draftInput = (value: Pick<AnalysisDraft, "note" | "state">) => JSON.stringify([value.state, value.note]);
const pending = (value: AnalysisDraft) => value.busy || !!value.attempt || draftInput(value) !== value.baseline;
function createStore() {
  const values = new Map<string, AnalysisDraft>(), listeners = new Set<() => void>();
  return { get: (key: string) => values.get(key),
    set(key: string, value: AnalysisDraft) { values.set(key, value); listeners.forEach(listener => listener()); },
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    count: () => [...values.values()].filter(pending).length };
}
const Context = createContext<ReturnType<typeof createStore> | null>(null);
export function SavedAnalysisDraftNotice() {
  const store = useContext(Context)!;
  const count = useSyncExternalStore(store.subscribe, store.count);
  return count ? <p className="journal-draft-notice" role="status">저장되지 않았거나 결과 확인이 필요한 분석 검토 {count}개가 있습니다. 같은 분석으로 돌아오면 이어 쓸 수 있습니다. 새로고침·로그아웃 시 임시 입력은 사라집니다.</p> : null;
}
export function SavedAnalysisDraftProvider({ children }: { children: ReactNode }) {
  const [store] = useState(createStore);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (store.count()) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [store]);
  return <Context.Provider value={store}>{children}</Context.Provider>;
}
export function SavedAnalysisDraftBoundary({ children }: { children: ReactNode }) {
  return useContext(Context) ? children : <SavedAnalysisDraftProvider><SavedAnalysisDraftNotice/>{children}</SavedAnalysisDraftProvider>;
}
export function useAnalysisDraft(key: string, initial: AnalysisDraft) {
  const store = useContext(Context)!; const fallback = useRef(initial);
  const get = () => store.get(key) ?? fallback.current;
  const value = useSyncExternalStore(store.subscribe, get);
  return { value, get, update: (change: (old: AnalysisDraft) => AnalysisDraft) => store.set(key, change(get())) };
}
