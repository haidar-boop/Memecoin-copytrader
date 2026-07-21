"use client";

import React from "react";
import Link from "next/link";
import {
  api,
  IngestionStats,
  TopWallet,
  MarketRegime,
  Trade,
} from "@/lib/api";
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
  shortAddr,
} from "@/components/ui";

export default function OverviewPage() {
  const ingestion = useApi<IngestionStats>(() =>
    api.get("/api/stats/ingestion"),
  );
  const regimes = useApi<MarketRegime[]>(() =>
    api.get("/api/evaluation/regimes", { limit: 1 }),
  );
  const top = useApi<TopWallet[]>(() =>
    api.get("/api/analytics/wallets/top", {
      by: "confidence_score",
      limit: 5,
    }),
  );
  const trades = useApi<Trade[]>(() =>
    api.get("/api/trades/recent", { limit: 8 }),
  );

  const c = ingestion.data?.counts;
  const regime = regimes.data?.[0];

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Overview</h1>

      <Section title="Ingestion">
        {ingestion.loading && <Loading />}
        {ingestion.error && <ErrorBox error={ingestion.error} />}
        {c && (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5">
            <StatTile label="Wallets" value={fmtNum(c.wallets?.count, 0)} />
            <StatTile label="Tokens" value={fmtNum(c.tokens?.count, 0)} />
            <StatTile
              label="Transactions"
              value={fmtNum(c.transactions?.count, 0)}
              sub={c.transactions?.estimate ? "estimate" : undefined}
            />
            <StatTile label="Trades" value={fmtNum(c.trades?.count, 0)} />
            <StatTile
              label="Queue depth"
              value={
                ingestion.data?.queue_depth === null
                  ? "—"
                  : fmtNum(ingestion.data?.queue_depth, 0)
              }
            />
          </div>
        )}
      </Section>

      <Section title="Latest Market Regime">
        {regimes.loading && <Loading />}
        {regimes.error && <ErrorBox error={regimes.error} />}
        {regimes.data && !regime && (
          <div className="card text-muted">No regime data yet.</div>
        )}
        {regime && (
          <div className="card">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="accent">{regime.regime}</Badge>
              {regime.high_volatility && <Badge tone="warn">high vol</Badge>}
              {regime.low_liquidity && <Badge tone="warn">low liq</Badge>}
              {regime.whale_accumulation && (
                <Badge tone="good">whale accum</Badge>
              )}
              {regime.panic_selling && <Badge tone="bad">panic</Badge>}
              {regime.launch_wave && <Badge tone="accent">launch wave</Badge>}
              {regime.trend_exhaustion && (
                <Badge tone="warn">exhaustion</Badge>
              )}
              <span className="ml-auto text-xs text-muted">
                {fmtTime(regime.ts)}
              </span>
            </div>
            {regime.description && (
              <p className="mt-2 text-sm text-muted">{regime.description}</p>
            )}
          </div>
        )}
      </Section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Section
          title="Top Wallets"
          right={
            <Link href="/wallets" className="text-sm text-accent">
              View all →
            </Link>
          }
        >
          {top.loading && <Loading />}
          {top.error && <ErrorBox error={top.error} />}
          {top.data && (
            <TableWrap>
              <thead>
                <tr>
                  <th className="th">Wallet</th>
                  <th className="th">Conf.</th>
                  <th className="th">PnL 30d</th>
                  <th className="th">Style</th>
                </tr>
              </thead>
              <tbody>
                {top.data.map((w) => (
                  <tr key={w.wallet_id}>
                    <td className="td">
                      <Link
                        href={`/wallets/${w.address}`}
                        className="text-accent hover:underline"
                      >
                        {shortAddr(w.address)}
                      </Link>
                    </td>
                    <td className="td">{fmtNum(w.confidence_score, 3)}</td>
                    <td className="td">
                      <Pnl v={w.pnl_30d_sol} />
                    </td>
                    <td className="td">
                      {w.style ? <Badge>{w.style}</Badge> : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          )}
        </Section>

        <Section
          title="Recent Trades"
          right={
            <Link href="/trades" className="text-sm text-accent">
              Live feed →
            </Link>
          }
        >
          {trades.loading && <Loading />}
          {trades.error && <ErrorBox error={trades.error} />}
          {trades.data && (
            <TableWrap>
              <thead>
                <tr>
                  <th className="th">Time</th>
                  <th className="th">Wallet</th>
                  <th className="th">Side</th>
                  <th className="th">SOL</th>
                </tr>
              </thead>
              <tbody>
                {trades.data.map((t) => (
                  <tr key={`${t.signature}-${t.event_index}`}>
                    <td className="td text-xs text-muted">
                      {fmtTime(t.block_time)}
                    </td>
                    <td className="td">{shortAddr(t.wallet_address)}</td>
                    <td className="td">
                      <Badge tone={t.side === "buy" ? "good" : "bad"}>
                        {t.side}
                      </Badge>
                    </td>
                    <td className="td">{fmtNum(t.quote_amount, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          )}
        </Section>
      </div>
    </div>
  );
}
