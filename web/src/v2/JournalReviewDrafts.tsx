import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";

export type Assessment = "not_assessed" | "supported" | "limited" | "unsupported";
export type ReviewState = "reviewing" | "accepted" | "held" | "rejected";
export type Judgment = { entry_id: string; assessment: Assessment; note: string };
export type ReviewSave = { request_id: string; expected_previous_id: string | null; expected_candidate_digest: string; state: ReviewState; entries: Judgment[]; note: string };
export type ReviewDraft = {
  entries: Judgment[]; note: string; base: string | null; baseline: string;
  busy: boolean; conflict: boolean; error: string; message: string;
  attempt: ReviewSave | null; savedReviewId: string | null;
};
export const reviewInput = (entries: Judgment[], note: string) => JSON.stringify({ entries, note });
export const needsReviewProtection = (draft: ReviewDraft) => draft.busy || !!draft.attempt || reviewInput(draft.entries, draft.note) !== draft.baseline;

// One store per authenticated App lifetime. No research text goes to browser storage.
function createStore() {
  const drafts = new Map<string, ReviewDraft>();
  const listeners = new Set<() => void>();
  return {
    get: (key: string) => drafts.get(key),
    set(key: string, value: ReviewDraft) { drafts.set(key, value); listeners.forEach(listener => listener()); },
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    pending: () => [...drafts.values()].filter(needsReviewProtection).length,
  };
}
const Context = createContext<ReturnType<typeof createStore> | null>(null);

export function JournalReviewDraftProvider({ children }: { children: ReactNode }) {
  const [store] = useState(createStore);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!store.pending()) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [store]);
  return <Context.Provider value={store}>{children}</Context.Provider>;
}

export function JournalReviewDraftNotice() {
  const store = useContext(Context)!;
  const pending = useSyncExternalStore(store.subscribe, store.pending);
  return pending ? <p className="journal-draft-notice" role="status">저장되지 않았거나 저장 결과를 확인해야 할 저널 검토 {pending}개가 있습니다. 같은 후보로 돌아오면 입력을 이어갈 수 있습니다. 새로고침·창 닫기·로그아웃·인증 만료 시 이 임시 입력은 사라집니다.</p> : null;
}

export function JournalReviewDraftBoundary({ children }: { children: ReactNode }) {
  const store = useContext(Context);
  return store ? children : <JournalReviewDraftProvider><JournalReviewDraftNotice/>{children}</JournalReviewDraftProvider>;
}

export function useJournalReviewDraft(key: string, initial: ReviewDraft) {
  const store = useContext(Context);
  if (!store) throw new Error("Journal review draft provider is required");
  const fallback = useRef(initial);
  const get = () => store.get(key) ?? fallback.current;
  const draft = useSyncExternalStore(store.subscribe, get);
  // Updates are synchronous so two mounted views share the same in-flight lock.
  const update = (change: (previous: ReviewDraft) => ReviewDraft) => store.set(key, change(get()));
  return { draft, get, update };
}
