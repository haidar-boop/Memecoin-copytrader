"use client";

import React, { useEffect, useMemo, useState } from "react";
import { api, NotificationItem } from "@/lib/api";
import { useLiveFeed } from "@/lib/ws";
import { fmtTime } from "./ui";

export function NotificationBell() {
  const [initial, setInitial] = useState<NotificationItem[]>([]);
  const [open, setOpen] = useState(false);
  const { frames } = useLiveFeed(100);

  useEffect(() => {
    api
      .get<NotificationItem[]>("/api/notifications", { limit: 30 })
      .then(setInitial)
      .catch(() => setInitial([]));
  }, []);

  const live = useMemo(
    () =>
      frames
        .filter((f) => f.channel && f.channel.includes("notif"))
        .map((f) => f.data as NotificationItem),
    [frames],
  );

  const items = useMemo(() => {
    const seen = new Set<string>();
    const merged: NotificationItem[] = [];
    let anon = 0;
    for (const n of [...live, ...initial]) {
      // Key on the stable identity (kind + timestamp + title), not a full
      // JSON stringify: the same event arriving once over REST and once over
      // the socket can differ in field ordering/extras and would otherwise
      // show as two entries.
      const key =
        n && (n.ts || n.kind)
          ? `${n.kind ?? ""}|${n.ts ?? ""}|${n.title ?? ""}`
          : `anon-${anon++}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(n);
    }
    return merged.slice(0, 40);
  }, [live, initial]);

  const count = items.length;

  return (
    <div className="relative">
      <button
        className="btn"
        onClick={() => setOpen((o) => !o)}
        aria-label="Notifications"
      >
        <span>🔔</span>
        {count > 0 && (
          <span className="rounded-full bg-accent px-1.5 text-xs text-black">
            {count > 40 ? "40+" : count}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 max-h-96 w-80 overflow-y-auto rounded-xl border border-edge bg-panel p-2 shadow-xl">
          <div className="px-2 py-1 text-xs uppercase tracking-wide text-muted">
            Notifications
          </div>
          {items.length === 0 && (
            <div className="px-2 py-3 text-sm text-muted">Nothing yet.</div>
          )}
          {items.map((n, i) => (
            <div
              key={i}
              className="rounded-lg px-2 py-2 text-sm hover:bg-panel2"
            >
              <div className="font-medium">
                {String(n.title || n.kind || "Event")}
              </div>
              {n.message !== undefined && (
                <div className="text-muted">{String(n.message)}</div>
              )}
              {n.ts !== undefined && (
                <div className="text-xs text-muted">{fmtTime(String(n.ts))}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
