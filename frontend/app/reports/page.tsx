"use client";

import React, { useState } from "react";
import { api, Report } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import {
  Section,
  Badge,
  ErrorBox,
  Loading,
  fmtTime,
} from "@/components/ui";

/** Minimal, safe markdown renderer: headings, bold, lists, paragraphs. */
function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const out: React.ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      out.push(
        <ul key={`ul-${out.length}`} className="mb-3 list-inside list-disc">
          {list.map((li, i) => (
            <li key={i}>{inline(li)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  const inline = (s: string): React.ReactNode => {
    const parts = s.split(/(\*\*[^*]+\*\*)/g);
    return parts.map((p, i) =>
      p.startsWith("**") && p.endsWith("**") ? (
        <strong key={i}>{p.slice(2, -2)}</strong>
      ) : (
        <React.Fragment key={i}>{p}</React.Fragment>
      ),
    );
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^#{1,6}\s/.test(line)) {
      flush();
      const level = line.match(/^#+/)![0].length;
      const content = line.replace(/^#+\s/, "");
      const cls =
        level <= 1
          ? "mt-4 mb-2 text-xl font-bold"
          : level === 2
            ? "mt-4 mb-2 text-lg font-semibold"
            : "mt-3 mb-1 font-semibold";
      out.push(
        <div key={out.length} className={cls}>
          {inline(content)}
        </div>,
      );
    } else if (/^\s*[-*]\s/.test(line)) {
      list.push(line.replace(/^\s*[-*]\s/, ""));
    } else if (line.trim() === "") {
      flush();
    } else {
      flush();
      out.push(
        <p key={out.length} className="mb-2 text-sm text-white/90">
          {inline(line)}
        </p>,
      );
    }
  }
  flush();
  return <div>{out}</div>;
}

function Sections({ sections }: { sections: Record<string, unknown> }) {
  return (
    <div className="space-y-3">
      {Object.entries(sections).map(([k, v]) => (
        <div key={k} className="rounded-lg border border-edge bg-panel2 p-3">
          <div className="mb-1 text-sm font-semibold capitalize">
            {k.replace(/_/g, " ")}
          </div>
          {typeof v === "string" ? (
            <div className="text-sm text-white/90">{v}</div>
          ) : (
            <pre className="overflow-x-auto text-xs text-muted">
              {JSON.stringify(v, null, 2)}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

export default function ReportsPage() {
  const list = useApi<Report[]>(() =>
    api.get("/api/reports", { kind: "weekly", limit: 20 }),
  );
  const [selected, setSelected] = useState<Report | null>(null);
  const latest = useApi<Report | null>(async () => {
    try {
      return await api.get<Report>("/api/reports/latest", { kind: "weekly" });
    } catch {
      return null;
    }
  });

  const active = selected || latest.data || null;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Reports</h1>
      <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
        <Section title="Weekly reports">
          {list.loading && <Loading what="reports" />}
          {list.error && <ErrorBox error={list.error} />}
          {list.data && (
            <div className="space-y-2">
              {list.data.length === 0 && (
                <div className="card text-muted">No reports yet.</div>
              )}
              {list.data.map((r) => {
                const isActive = active?.id === r.id;
                return (
                  <button
                    key={r.id}
                    onClick={() => setSelected(r)}
                    className={`w-full rounded-lg border p-3 text-left text-sm transition-colors ${
                      isActive
                        ? "border-accent bg-accent/10"
                        : "border-edge bg-panel hover:bg-panel2"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <Badge tone="accent">{r.kind}</Badge>
                      <span className="text-xs text-muted">
                        {fmtTime(r.generated_at)}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-muted">
                      {fmtTime(r.window_start)} → {fmtTime(r.window_end)}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </Section>

        <Section title={active ? "Report detail" : "Latest report"}>
          {latest.loading && !selected && <Loading />}
          {!active && !latest.loading && (
            <div className="card text-muted">
              No weekly report available. Select one from the list.
            </div>
          )}
          {active && (
            <div className="card">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <div className="text-lg font-semibold capitalize">
                    {active.kind} report
                  </div>
                  <div className="text-xs text-muted">
                    {fmtTime(active.window_start)} → {fmtTime(active.window_end)}
                  </div>
                </div>
                <span className="text-xs text-muted">
                  {fmtTime(active.generated_at)}
                </span>
              </div>
              {active.summary && (
                <p className="mb-4 text-sm text-white/90">{active.summary}</p>
              )}
              {active.markdown ? (
                <Markdown text={active.markdown} />
              ) : active.sections ? (
                <Sections sections={active.sections} />
              ) : (
                <div className="text-sm text-muted">
                  Report has no body content.
                </div>
              )}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}
