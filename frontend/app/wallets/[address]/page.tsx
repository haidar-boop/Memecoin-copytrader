"use client";

import React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, Wallet, WalletStats, WalletStatsSnapshot } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Sparkline } from "@/components/Sparkline";
import { StarButton } from "@/components/StarButton";
import {
  StatTile,
  Section,
  Badge,
  Pnl,
  ErrorBox,
  Loading,
  fmtNum,
  fmtPct,
  fmtTime,
} from "@/components/ui";

function ConfidenceComponents({
  components,
}: {
  components: Array<Record<string, unknown>> | null;
}) {
  if (!components || components.length === 0) {
    return <div className="text-sm text-muted">No component breakdown.</div>;
  }
  return (
    <div className="space-y-2">
      {components.map((comp, i) => {
        const name = String(comp.name ?? comp.label ?? `factor ${i + 1}`);
        const value = comp.value ?? comp.score ?? comp.contribution;
        const weight = comp.weight;
        const num = Number(value);
        const pct = isFinite(num)
          ? Math.max(0, Math.min(1, Math.abs(num)))
          : 0;
        return (
          <div key={i}>
            <div className="flex justify-between text-sm">
              <span>{name}</span>
              <span className="text-muted">
                {isFinite(num) ? num.toFixed(3) : "—"}
                {weight !== undefined && ` (w ${fmtNum(Number(weight), 2)})`}
              </span>
            </div>
            <div className="mt-1 h-2 overflow-hidden rounded bg-panel2">
              <div
                className={num < 0 ? "h-full bg-bad" : "h-full bg-accent"}
                style={{ width: `${pct * 100}%` }}
              />
            </div>
            {comp.description !== undefined && (
              <div className="mt-0.5 text-xs text-muted">
                {String(comp.description)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function WalletDetailPage() {
  const params = useParams<{ address: string }>();
  const address = params.address;
  // Local override so the banner and star stay in sync after a toggle,
  // instead of reverting to the (now stale) fetched value.
  const [trackedOverride, setTrackedOverride] = React.useState<boolean | null>(
    null,
  );

  const wallet = useApi<Wallet>(
    () => api.get(`/api/wallets/${address}`),
    [address],
  );
  const isTracked = trackedOverride ?? wallet.data?.is_tracked ?? false;
  const stats = useApi<WalletStats>(
    () => api.get(`/api/wallets/${address}/stats`),
    [address],
  );
  const history = useApi<WalletStatsSnapshot[]>(
    () => api.get(`/api/wallets/${address}/stats/history`, { limit: 60 }),
    [address],
  );

  const s = stats.data;
  const snaps = history.data
    ? [...history.data].sort(
        (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
      )
    : [];

  return (
    <div>
      <div className="mb-6">
        <Link href="/wallets" className="text-sm text-accent">
          ← Rankings
        </Link>
        <h1 className="mt-1 flex items-start gap-2 break-all font-mono text-xl font-bold">
          {wallet.data && (
            <StarButton
              address={address}
              tracked={isTracked}
              onChange={setTrackedOverride}
              className="mt-0.5 shrink-0"
            />
          )}
          <span>{address}</span>
        </h1>
        {wallet.data && isTracked && (
          <div className="mt-1 text-xs text-amber-400">
            Tracked — the copy engine follows this wallet&apos;s buys.
          </div>
        )}
        {wallet.error && (
          <div className="mt-1 text-xs text-muted">
            Tracked status unavailable: {wallet.error.message}
          </div>
        )}
      </div>

      {stats.loading && <Loading what="wallet stats" />}
      {stats.error && <ErrorBox error={stats.error} />}

      {s && (
        <>
          <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
            <StatTile
              label="Confidence"
              value={fmtNum(s.confidence_score, 3)}
              sub={s.style ? <Badge>{s.style}</Badge> : undefined}
            />
            <StatTile
              label="Total PnL (SOL)"
              value={<Pnl v={s.total_pnl_sol} />}
              tone={
                s.total_pnl_sol == null
                  ? undefined
                  : Number(s.total_pnl_sol) >= 0
                    ? "good"
                    : "bad"
              }
            />
            <StatTile label="Win rate" value={fmtPct(s.win_rate)} />
            <StatTile
              label="Closed positions"
              value={fmtNum(s.closed_position_count, 0)}
            />
            <StatTile label="Trades" value={fmtNum(s.trade_count, 0)} />
            <StatTile label="Avg ROI" value={fmtPct(s.avg_roi)} />
            <StatTile
              label="Profit factor"
              value={fmtNum(s.profit_factor, 2)}
            />
            <StatTile
              label="Max drawdown"
              value={fmtPct(s.max_drawdown_pct)}
            />
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Section title="Confidence breakdown">
              <div className="card">
                <ConfidenceComponents components={s.confidence_components} />
              </div>
            </Section>

            <Section title="Snapshot history">
              <div className="card">
                {history.loading && <Loading />}
                {history.error && <ErrorBox error={history.error} />}
                {snaps.length > 1 ? (
                  <div className="space-y-4">
                    <div>
                      <div className="mb-1 text-xs uppercase text-muted">
                        Confidence
                      </div>
                      <Sparkline
                        values={snaps.map((x) => x.confidence_score)}
                        width={320}
                      />
                    </div>
                    <div>
                      <div className="mb-1 text-xs uppercase text-muted">
                        Total PnL (SOL)
                      </div>
                      <Sparkline
                        values={snaps.map((x) => x.total_pnl_sol)}
                        width={320}
                        stroke="#34d399"
                      />
                    </div>
                    <div className="text-xs text-muted">
                      {snaps.length} snapshots · latest {fmtTime(
                        snaps[snaps.length - 1].ts,
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="text-sm text-muted">
                    Not enough snapshot history to chart.
                  </div>
                )}
              </div>
            </Section>
          </div>

          <Section title="Details">
            <div className="card grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-3">
              <Row k="PnL 7d" v={<Pnl v={s.pnl_7d_sol} />} />
              <Row k="PnL 30d" v={<Pnl v={s.pnl_30d_sol} />} />
              <Row k="Total volume" v={fmtNum(s.total_volume_sol, 2)} />
              <Row k="Median ROI" v={fmtPct(s.median_roi)} />
              <Row k="Avg position (SOL)" v={fmtNum(s.avg_position_sol, 3)} />
              <Row k="Max position (SOL)" v={fmtNum(s.max_position_sol, 3)} />
              <Row k="Trades / day" v={fmtNum(s.trades_per_day, 2)} />
              <Row
                k="Avg hold"
                v={
                  s.avg_hold_seconds
                    ? `${fmtNum(s.avg_hold_seconds / 60, 1)} min`
                    : "—"
                }
              />
              <Row k="Computed" v={fmtTime(s.computed_at)} />
            </div>
          </Section>
        </>
      )}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between border-b border-edge/40 py-1">
      <span className="text-muted">{k}</span>
      <span>{v}</span>
    </div>
  );
}
