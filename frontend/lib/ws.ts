"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "./api";
import { getToken } from "./auth";

export interface LiveFrame {
  channel: string;
  data: unknown;
}

function wsUrl(): string {
  const base = API_BASE.replace(/^http/, "ws").replace(/\/?$/, "");
  // The backend gates /ws/live when auth is configured; browsers can't set a
  // WebSocket Authorization header, so the token rides as a query param.
  const token = getToken();
  return token
    ? `${base}/ws/live?token=${encodeURIComponent(token)}`
    : `${base}/ws/live`;
}

/**
 * Subscribe to /ws/live. Returns the most recent frames (newest first, capped)
 * and a connection status. Auto-reconnects with backoff.
 */
export function useLiveFeed(max = 200): {
  frames: LiveFrame[];
  status: "connecting" | "open" | "closed";
} {
  const [frames, setFrames] = useState<LiveFrame[]>([]);
  const [status, setStatus] =
    useState<"connecting" | "open" | "closed">("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const stoppedRef = useRef(false);
  const attemptRef = useRef(0);

  useEffect(() => {
    stoppedRef.current = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (stoppedRef.current) return;
      setStatus("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setStatus("open");
      };
      ws.onmessage = (ev) => {
        try {
          const frame = JSON.parse(ev.data) as LiveFrame;
          setFrames((prev) => [frame, ...prev].slice(0, max));
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        setStatus("closed");
        scheduleReconnect();
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
    };

    const scheduleReconnect = () => {
      if (stoppedRef.current) return;
      attemptRef.current += 1;
      const delay = Math.min(1000 * 2 ** attemptRef.current, 15000);
      timer = setTimeout(connect, delay);
    };

    connect();

    return () => {
      stoppedRef.current = true;
      if (timer) clearTimeout(timer);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          /* ignore */
        }
      }
    };
  }, [max]);

  return { frames, status };
}
