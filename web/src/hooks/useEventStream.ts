import { useEffect, useMemo, useState } from "react";

import { getApiBase, RunEvent } from "../lib/api";

export function useEventStream(runId: string | undefined, enabled: boolean) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const eventUrl = useMemo(() => {
    if (!runId) {
      return null;
    }
    return `${getApiBase()}/runs/${runId}/events`;
  }, [runId]);

  useEffect(() => {
    if (!eventUrl || !enabled) {
      return;
    }

    const source = new EventSource(eventUrl, { withCredentials: true });
    source.onopen = () => setConnected(true);
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as RunEvent;
        setEvents((previous) => {
          if (previous.some((item) => item.timestamp === payload.timestamp && item.type === payload.type)) {
            return previous;
          }
          return [...previous, payload];
        });
      } catch {
        // Ignore malformed event payloads.
      }
    };
    source.onerror = () => {
      setConnected(false);
    };

    return () => {
      source.close();
      setConnected(false);
    };
  }, [enabled, eventUrl]);

  return { events, connected };
}
