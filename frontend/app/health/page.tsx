"use client";

import React from "react";
import {
  api,
  IngestionStats,
  ModelPerformance,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import {
  StatTile,
  Section,
  Badge,
  ErrorBox,
  Loading,
  TableWrap,
  fmtNum,
  fmtPct,
  fmtTime,
} from "@/components/ui";

export default function HealthPage() {
  const ingestion = useApi<IngestionStats>(() =>
    api.get("/api/stats/ingestion"),
  );
  const models = useApi<ModelPerformance[]>(() =>
    api.get("/api/evaluation/models"),
  );

  const c = ingestion.data?.counts;
  const checkpoint = ingestion.data?.listener_checkpoint || {};
  const cpEntries = Object.entries(checkpoint);

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">System Health</h1>

      <Section title="Ingestion counts">
        {ingestion.loading && <Loading />}
        {ingestion.error && <ErrorBox error={ingestion.error} />}
        {c && (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
            {Object.entries(c).map(([name, v]) => (
              <StatTile
                key={name}
                label={name.replace(/_/g, " ")}
                value={fmtNum(v.count, 0)}
                sub={v.estimate ? "estimate" : undefined}
              />
            ))}
            <StatTile
              label="queue depth"
              value={
                ingestion.data?.queue_depth === null
                  ? "—"
                  : fmtNum(ingestion.data?.queue_depth, 0)
              }
            />
          </div>
        )}
      </Section>

      <Section title="Worker freshness (listener checkpoint)">
        {ingestion.data && cpEntries.length === 0 && (
          <div className="card text-muted">
            No listener checkpoint reported (Redis may be unavailable or the
            worker has not checkpointed yet).
          </div>
        )}
        {cpEntries.length > 0 && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">Key</th>
                <th className="th">Value</th>
              </tr>
            </thead>
            <tbody>
              {cpEntries.map(([k, v]) => (
                <tr key={k}>
                  <td className="td font-mono">{k}</td>
                  <td className="td font-mono text-muted">{String(v)}</td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Section>

      <Section title="Model performance">
        {models.loading && <Loading what="models" />}
        {models.error && <ErrorBox error={models.error} />}
        {models.data && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">Model</th>
                <th className="th">Window</th>
                <th className="th">Resolved</th>
                <th className="th">AUC</th>
                <th className="th">Brier</th>
                <th className="th">Accuracy</th>
                <th className="th">Base rate</th>
                <th className="th">Evaluated</th>
              </tr>
            </thead>
            <tbody>
              {models.data.map((m) => (
                <tr key={m.id}>
                  <td className="td">
                    {m.model_name}
                    <span className="ml-2 text-xs text-muted">
                      #{m.model_id}
                    </span>
                  </td>
                  <td className="td text-muted">{m.window_days}d</td>
                  <td className="td">{fmtNum(m.resolved_count, 0)}</td>
                  <td className="td">
                    {m.auc != null ? (
                      <Badge
                        tone={
                          m.auc >= 0.6
                            ? "good"
                            : m.auc >= 0.5
                              ? "warn"
                              : "bad"
                        }
                      >
                        {fmtNum(m.auc, 3)}
                      </Badge>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="td">{fmtNum(m.brier, 3)}</td>
                  <td className="td">{fmtPct(m.accuracy)}</td>
                  <td className="td">{fmtPct(m.base_rate)}</td>
                  <td className="td text-xs text-muted">{fmtTime(m.ts)}</td>
                </tr>
              ))}
              {models.data.length === 0 && (
                <tr>
                  <td className="td text-muted" colSpan={8}>
                    No model performance records.
                  </td>
                </tr>
              )}
            </tbody>
          </TableWrap>
        )}
      </Section>
    </div>
  );
}
