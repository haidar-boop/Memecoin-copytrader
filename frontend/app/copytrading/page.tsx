"use client";

import React, { useState } from "react";
import { api, Decision, CopyPosition, CopyStatus } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import {
  StatTile,
  Section,
  Badge,
  Pnl,
  ErrorBox,
  Loading,
  TableWrap,
  fmtNum,
  fmtTime,
} from "@/components/ui";

function DecisionRow({ d }: { d: Decision }) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    (d.reasons && d.reasons.length > 0) ||
    (d.factors && d.factors.length > 0);
  return (
    <>
      <tr
        className={hasDetail ? "cursor-pointer hover:bg-panel2/50" : ""}
        onClick={() => hasDetail && setOpen((o) => !o)}
      >
        <td className="td text-xs text-muted">{fmtTime(d.created_at)}</td>
        <td className="td">
          <Badge tone={d.decision === "copy" ? "good" : "neutral"}>
            {d.decision}
          </Badge>
        </td>
        <td className="td">
          <Badge tone={d.side === "buy" ? "good" : "bad"}>{d.side}</Badge>
        </td>
        <td className="td">{fmtNum(d.confidence_score, 3)}</td>
        <td className="td">{fmtNum(d.risk_score, 3)}</td>
        <td className="td">{fmtNum(d.p_profit, 3)}</td>
        <td className="td">{fmtNum(d.size_sol, 3)}</td>
        <td className="td text-muted">
          {hasDetail ? (open ? "▲" : "▼") : ""}
        </td>
      </tr>
      {open && hasDetail && (
        <tr>
          <td className="td bg-panel2/30" colSpan={8}>
            {d.reasons && d.reasons.length > 0 && (
              <div className="mb-2">
                <div className="text-xs uppercase text-muted">Reasons</div>
                <ul className="list-inside list-disc text-sm">
                  {d.reasons.map((r, i) => (
                    <li key={i}>{String(r)}</li>
                  ))}
                </ul>
              </div>
            )}
            {d.factors && d.factors.length > 0 && (
              <div>
                <div className="text-xs uppercase text-muted">Factors</div>
                <div className="flex flex-wrap gap-2 text-xs">
                  {d.factors.map((f, i) => (
                    <span
                      key={i}
                      className="rounded bg-panel px-2 py-1 font-mono"
                    >
                      {JSON.stringify(f)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function CopytradingPage() {
  const status = useApi<CopyStatus>(() => api.get("/api/copytrading/status"));
  const decisions = useApi<Decision[]>(() =>
    api.get("/api/copytrading/decisions", { limit: 50 }),
  );
  const positions = useApi<CopyPosition[]>(() =>
    api.get("/api/copytrading/positions", { limit: 50 }),
  );
  const [stopping, setStopping] = useState(false);
  const [stopMsg, setStopMsg] = useState<string | null>(null);

  const s = status.data;
  const stopped = !!s?.emergency_stop;

  const emergencyStop = async () => {
    if (!confirm("Trip the emergency stop? This halts all copy trading."))
      return;
    setStopping(true);
    setStopMsg(null);
    try {
      await api.post("/api/copytrading/emergency-stop");
      setStopMsg("Emergency stop engaged.");
      status.reload();
    } catch (e) {
      setStopMsg(e instanceof Error ? e.message : "Failed");
    } finally {
      setStopping(false);
    }
  };

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Copy Trading</h1>

      <Section title="Risk & Safety">
        {status.loading && <Loading what="status" />}
        {status.error && <ErrorBox error={status.error} />}
        {s && (
          <>
            <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-5">
              <StatTile
                label="Emergency stop"
                value={stopped ? "TRIPPED" : "clear"}
                tone={stopped ? "bad" : "good"}
                sub={stopped ? String(s.emergency_stop) : undefined}
              />
              <StatTile label="Open positions" value={s.open_positions} />
              <StatTile
                label="Exposure (SOL)"
                value={fmtNum(s.exposure_sol, 3)}
              />
              <StatTile
                label="Daily PnL (SOL)"
                value={<Pnl v={s.daily_realized_pnl_sol} />}
              />
              <StatTile
                label="Mode"
                value={s.mode}
                sub={s.enabled ? "enabled" : "disabled"}
                tone={s.enabled ? "good" : "warn"}
              />
            </div>
            <div className="flex items-center gap-3">
              <button
                className="btn btn-danger"
                onClick={emergencyStop}
                disabled={stopping || stopped}
              >
                {stopping ? "Stopping…" : "⛔ Emergency stop"}
              </button>
              {stopMsg && <span className="text-sm text-muted">{stopMsg}</span>}
            </div>
          </>
        )}
      </Section>

      <Section title="Open & recent positions">
        {positions.loading && <Loading what="positions" />}
        {positions.error && <ErrorBox error={positions.error} />}
        {positions.data && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">Opened</th>
                <th className="th">Token</th>
                <th className="th">Mode</th>
                <th className="th">Status</th>
                <th className="th">Spent</th>
                <th className="th">Sold</th>
                <th className="th">Realized PnL</th>
              </tr>
            </thead>
            <tbody>
              {positions.data.map((p) => (
                <tr key={p.id}>
                  <td className="td text-xs text-muted">
                    {fmtTime(p.opened_at)}
                  </td>
                  <td className="td">#{p.token_id}</td>
                  <td className="td text-muted">{p.mode}</td>
                  <td className="td">
                    <Badge tone={p.status === "open" ? "accent" : "neutral"}>
                      {p.status}
                    </Badge>
                  </td>
                  <td className="td">{fmtNum(p.spent_sol, 3)}</td>
                  <td className="td">{fmtNum(p.sold_sol, 3)}</td>
                  <td className="td">
                    <Pnl v={p.realized_pnl_sol} />
                  </td>
                </tr>
              ))}
              {positions.data.length === 0 && (
                <tr>
                  <td className="td text-muted" colSpan={7}>
                    No positions.
                  </td>
                </tr>
              )}
            </tbody>
          </TableWrap>
        )}
      </Section>

      <Section title="Decisions">
        {decisions.loading && <Loading what="decisions" />}
        {decisions.error && <ErrorBox error={decisions.error} />}
        {decisions.data && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">Time</th>
                <th className="th">Decision</th>
                <th className="th">Side</th>
                <th className="th">Confidence</th>
                <th className="th">Risk</th>
                <th className="th">P(profit)</th>
                <th className="th">Size SOL</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {decisions.data.map((d) => (
                <DecisionRow key={d.id} d={d} />
              ))}
              {decisions.data.length === 0 && (
                <tr>
                  <td className="td text-muted" colSpan={8}>
                    No decisions recorded.
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
