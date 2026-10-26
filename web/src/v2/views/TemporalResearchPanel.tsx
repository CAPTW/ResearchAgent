import { useTemporalMemory } from "../useTemporalMemory";
import { TemporalMemoryView } from "./TemporalMemoryPanel";
import { TemporalAnalysisPanel } from "./TemporalAnalysisPanel";

export function TemporalResearchPanel({ topicId }: { topicId: string }) {
  const memory = useTemporalMemory(topicId);
  return <div className="v2-screen-stack">
    <TemporalMemoryView state={memory} />
    <TemporalAnalysisPanel topicId={topicId} snapshotResource={{
      ...memory.list,
      phase: memory.busy ? "loading" : memory.list.phase,
      data: memory.list.data ? { ...memory.list.data, items: memory.selected ? [memory.selected] : [] } : null,
    }} />
  </div>;
}
