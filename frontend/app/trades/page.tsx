"use client";

import React, { useEffect, useMemo, useState } from "react";
import { api, Trade } from "@/lib/api";
import { useLiveFeed } from "@/lib/ws";
import {
  Section,
  Badge,
  ErrorBox,
  TableWrap,
  fmtNum,
  fmtTime,
  shortAddr,
} from "@/components/ui";

interface AnyTrade {
  signature?: string;
  event_index?: number;
  block_time?: string;
  wallet_address?: string;
  token_mint?: string;
  dex?: string;
  side?: string;
  quote_amount?: number | string;
  price_usd?: number | string | null;
  [k: string]: unknown;
}

export default function TradesPage() {
  const [initial, setInitial] = useState<Trade[]>([]);
  const [err, setErr] = useState<Error | null>(null);
  const { frames, status } = useLiveFeed(300);

  useEffect(() => {
    api
      .get<Trade[]>("/api/trades/recent", { limit: 30 })
      .then(setInitial)
      .catch((e) => setErr(e instanceof Error ? e : new Error(String(e))));
  }, []);

  const liveTrades = useMemo<AnyTrade[]>(
    () =>
      frames
        .filter((f) => f.channel && f.channel.includes("trade"))
        .map((f) => f.data as AnyTrade),
    [frames],
  );

  const rows = useMemo<AnyTrade[]>(() => {
    const merged: AnyTrade[] = [
      ...liveTrades,
      ...(initial as unknown as AnyTrade[]),
    ];
    const seen = new Set<string>();
    const out: AnyTrade[] = [];
    let anon = 0;
    for (const t of merged) {
      // Only dedup rows that actually carry an identity; frames without a
      // signature get a unique key so distinct trades are never collapsed
      // into one "undefined-undefined" row.
      const key =
        t.signature != null
          ? `${t.signature}-${t.event_index ?? 0}`
          : `anon-${anon++}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(t);
    }
    return out.slice(0, 200);
  }, [liveTrades, initial]);

  const statusTone =
    status === "open" ? "good" : status === "connecting" ? "warn" : "bad";

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold">Live Trades</h1>
        <Badge tone={statusTone}>
          {status === "open"
            ? "● live"
            : status === "connecting"
              ? "connecting…"
              : "disconnected"}
        </Badge>
      </div>

      <Section title="Feed">
        {err && <ErrorBox error={err} />}
        <TableWrap>
          <thead>
            <tr>
              <th className="th">Time</th>
              <th className="th">Wallet</th>
              <th className="th">Token</th>
              <th className="th">DEX</th>
              <th className="th">Side</th>
              <th className="th">SOL</th>
              <th className="th">USD px</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => (
              <tr
                key={`${t.signature}-${t.event_index}-${i}`}
                className={i === 0 && liveTrades.length > 0 ? "bg-accent/5" : ""}
              >
                <td className="td text-xs text-muted">
                  {fmtTime(t.block_time)}
                </td>
                <td className="td font-mono">
                  {shortAddr(t.wallet_address)}
                </td>
                <td className="td font-mono">{shortAddr(t.token_mint)}</td>
                <td className="td text-muted">{t.dex || "—"}</td>
                <td className="td">
                  <Badge tone={t.side === "buy" ? "good" : "bad"}>
                    {t.side || "—"}
                  </Badge>
                </td>
                <td className="td">{fmtNum(t.quote_amount as number, 3)}</td>
                <td className="td text-muted">
                  {t.price_usd != null
                    ? fmtNum(t.price_usd as number, 6)
                    : "—"}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td className="td text-muted" colSpan={7}>
                  Waiting for trades…
                </td>
              </tr>
            )}
          </tbody>
        </TableWrap>
      </Section>
    </div>
  );
}
