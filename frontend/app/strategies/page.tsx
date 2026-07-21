"use client";

import React from "react";
import { api, StrategyCluster, MarketRegime } from "@/lib/api";
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
  fmtTime,
} from "@/components/ui";

export default function StrategiesPage() {
  const strategies = useApi<StrategyCluster[]>(() =>
    api.get("/api/analytics/strategies"),
  );
  const regimes = useApi<MarketRegime[]>(() =>
    api.get("/api/evaluation/regimes", { limit: 12 }),
  );

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Strategies & Regimes</h1>

      <Section title="Strategy clusters">
        {strategies.loading && <Loading what="strategies" />}
        {strategies.error && <ErrorBox error={strategies.error} />}
        {strategies.data && (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {strategies.data.map((c) => {
              const st = c.latest_stat;
              return (
                <div key={c.id} className="card">
                  <div className="flex items-center justify-between">
                    <div className="font-semibold">{c.name}</div>
                    <Badge tone="accent">{c.member_count} wallets</Badge>
                  </div>
                  {c.description && (
                    <p className="mt-1 text-sm text-muted">{c.description}</p>
                  )}
                  {st ? (
                    <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                      <Kv k="Win rate" v={fmtPct(st.win_rate)} />
                      <Kv k="Avg ROI" v={fmtPct(st.avg_roi)} />
                      <Kv
                        k="Total PnL"
                        v={<Pnl v={st.total_pnl_sol} />}
                      />
                      <Kv
                        k="Profit factor"
                        v={fmtNum(st.profit_factor, 2)}
                      />
                      <Kv
                        k="Closed"
                        v={fmtNum(st.closed_positions, 0)}
                      />
                      <Kv k="Window" v={`${st.window_days}d`} />
                    </div>
                  ) : (
                    <div className="mt-3 text-sm text-muted">
                      No recent stats.
                    </div>
                  )}
                </div>
              );
            })}
            {strategies.data.length === 0 && (
              <div className="card text-muted">No strategy clusters yet.</div>
            )}
          </div>
        )}
      </Section>

      <Section title="Recent market regimes">
        {regimes.loading && <Loading what="regimes" />}
        {regimes.error && <ErrorBox error={regimes.error} />}
        {regimes.data && (
          <TableWrap>
            <thead>
              <tr>
                <th className="th">Time</th>
                <th className="th">Regime</th>
                <th className="th">Signals</th>
                <th className="th">Window</th>
              </tr>
            </thead>
            <tbody>
              {regimes.data.map((r) => (
                <tr key={r.id}>
                  <td className="td text-xs text-muted">{fmtTime(r.ts)}</td>
                  <td className="td">
                    <Badge tone="accent">{r.regime}</Badge>
                  </td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1">
                      {r.high_volatility && <Badge tone="warn">high vol</Badge>}
                      {r.low_liquidity && <Badge tone="warn">low liq</Badge>}
                      {r.whale_accumulation && (
                        <Badge tone="good">whale</Badge>
                      )}
                      {r.panic_selling && <Badge tone="bad">panic</Badge>}
                      {r.launch_wave && <Badge tone="accent">launch</Badge>}
                      {r.trend_exhaustion && (
                        <Badge tone="warn">exhaustion</Badge>
                      )}
                    </div>
                  </td>
                  <td className="td text-muted">{r.window_minutes}m</td>
                </tr>
              ))}
              {regimes.data.length === 0 && (
                <tr>
                  <td className="td text-muted" colSpan={4}>
                    No regime records.
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

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-muted">{k}</div>
      <div>{v}</div>
    </div>
  );
}
