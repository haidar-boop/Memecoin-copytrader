"use client";

import React, { useState } from "react";
import Link from "next/link";
import { api, TopWallet } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import {
  Section,
  Badge,
  Pnl,
  ErrorBox,
  Loading,
  TableWrap,
  fmtNum,
  fmtPct,
  shortAddr,
} from "@/components/ui";

const SORTS: Array<{ key: string; label: string }> = [
  { key: "confidence_score", label: "Confidence" },
  { key: "total_pnl_sol", label: "Total PnL" },
  { key: "pnl_30d_sol", label: "PnL 30d" },
  { key: "win_rate", label: "Win rate" },
];

export default function WalletsPage() {
  const [by, setBy] = useState("confidence_score");
  const [minClosed, setMinClosed] = useState(0);
  const { data, error, loading } = useApi<TopWallet[]>(
    () =>
      api.get("/api/analytics/wallets/top", {
        by,
        limit: 100,
        min_closed: minClosed,
      }),
    [by, minClosed],
  );

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Wallet Rankings</h1>
      <Section
        title="Tracked wallets"
        right={
          <div className="flex items-center gap-2">
            <label className="text-xs text-muted">min closed</label>
            <input
              type="number"
              min={0}
              value={minClosed}
              onChange={(e) => setMinClosed(Number(e.target.value) || 0)}
              className="input w-20"
            />
          </div>
        }
      >
        <div className="mb-3 flex flex-wrap gap-2">
          {SORTS.map((s) => (
            <button
              key={s.key}
              onClick={() => setBy(s.key)}
              className={`btn ${by === s.key ? "border-accent text-accent" : ""}`}
            >
              {s.label} ▾
            </button>
          ))}
        </div>
        {loading && <Loading what="wallets" />}
        {error && <ErrorBox error={error} />}
        {data && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">#</th>
                <th className="th">Wallet</th>
                <th className="th">Confidence</th>
                <th className="th">Total PnL</th>
                <th className="th">PnL 30d</th>
                <th className="th">Win rate</th>
                <th className="th">Closed</th>
                <th className="th">Style</th>
              </tr>
            </thead>
            <tbody>
              {data.map((w, i) => (
                <tr key={w.wallet_id} className="hover:bg-panel2/50">
                  <td className="td text-muted">{i + 1}</td>
                  <td className="td">
                    <Link
                      href={`/wallets/${w.address}`}
                      className="font-mono text-accent hover:underline"
                    >
                      {shortAddr(w.address, 6)}
                    </Link>
                  </td>
                  <td className="td">{fmtNum(w.confidence_score, 3)}</td>
                  <td className="td">
                    <Pnl v={w.total_pnl_sol} />
                  </td>
                  <td className="td">
                    <Pnl v={w.pnl_30d_sol} />
                  </td>
                  <td className="td">{fmtPct(w.win_rate)}</td>
                  <td className="td">{w.closed_position_count}</td>
                  <td className="td">
                    {w.style ? <Badge>{w.style}</Badge> : "—"}
                  </td>
                </tr>
              ))}
              {data.length === 0 && (
                <tr>
                  <td className="td text-muted" colSpan={8}>
                    No wallets match.
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
