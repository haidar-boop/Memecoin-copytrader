import React from "react";

export function fmtNum(
  v: number | string | null | undefined,
  digits = 2,
): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "string" ? Number(v) : v;
  if (!isFinite(n)) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

export function fmtTime(v: string | null | undefined): string {
  if (!v) return "—";
  const d = new Date(v);
  if (isNaN(d.getTime())) return v;
  return d.toLocaleString();
}

export function shortAddr(a: string | null | undefined, n = 4): string {
  if (!a) return "—";
  if (a.length <= n * 2 + 3) return a;
  return `${a.slice(0, n)}…${a.slice(-n)}`;
}

export function StatTile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "good" | "bad" | "warn";
}) {
  const toneClass =
    tone === "good"
      ? "text-good"
      : tone === "bad"
        ? "text-bad"
        : tone === "warn"
          ? "text-warn"
          : "text-white";
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {sub !== undefined && (
        <div className="mt-1 text-xs text-muted">{sub}</div>
      )}
    </div>
  );
}

export function Pnl({ v }: { v: number | string | null | undefined }) {
  const n = v === null || v === undefined || v === "" ? null : Number(v);
  if (n === null || !isFinite(n)) return <span className="text-muted">—</span>;
  const cls = n > 0 ? "text-good" : n < 0 ? "text-bad" : "text-muted";
  return (
    <span className={cls}>
      {n > 0 ? "+" : ""}
      {fmtNum(n, 3)}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn" | "accent";
}) {
  const map: Record<string, string> = {
    neutral: "bg-panel2 text-muted border border-edge",
    good: "bg-good/10 text-good border border-good/30",
    bad: "bg-bad/10 text-bad border border-bad/30",
    warn: "bg-warn/10 text-warn border border-warn/30",
    accent: "bg-accent/10 text-accent border border-accent/30",
  };
  return <span className={`pill ${map[tone]}`}>{children}</span>;
}

export function Section({
  title,
  children,
  right,
}: {
  title: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <section className="mb-8">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function TableWrap({ children }: { children: React.ReactNode }) {
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full border-collapse">{children}</table>
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="card border-bad/40 text-bad">
      <div className="font-semibold">Failed to load</div>
      <div className="mt-1 text-sm">{msg}</div>
      <div className="mt-1 text-xs text-muted">
        Check that the API is reachable at {""}
        {process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}.
      </div>
    </div>
  );
}

export function Loading({ what = "data" }: { what?: string }) {
  return <div className="text-sm text-muted">Loading {what}…</div>;
}
