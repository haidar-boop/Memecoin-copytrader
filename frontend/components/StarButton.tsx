"use client";

import React, { useEffect, useState } from "react";
import { trackWallet, untrackWallet } from "@/lib/api";

/**
 * Star toggle for manually tracking a wallet. Optimistic: flips instantly,
 * reverts (and surfaces the error as a title tooltip) if the API call fails.
 */
export function StarButton({
  address,
  tracked,
  onChange,
  className = "",
}: {
  address: string;
  tracked: boolean;
  onChange?: (tracked: boolean) => void;
  className?: string;
}) {
  const [isTracked, setIsTracked] = useState(tracked);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setIsTracked(tracked), [tracked]);

  async function toggle(e: React.MouseEvent) {
    // The star often lives inside a row whose cells link elsewhere.
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    const next = !isTracked;
    setIsTracked(next);
    setBusy(true);
    setError(null);
    try {
      if (next) await trackWallet(address);
      else await untrackWallet(address);
      onChange?.(next);
    } catch (err) {
      setIsTracked(!next);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={toggle}
      disabled={busy}
      aria-label={isTracked ? "Unstar wallet" : "Star wallet"}
      aria-pressed={isTracked}
      title={
        error
          ? `Failed: ${error}`
          : isTracked
            ? "Tracked — the copy engine follows this wallet. Tap to unstar."
            : "Star to have the copy engine follow this wallet."
      }
      className={`text-lg leading-none transition-opacity ${
        busy ? "opacity-40" : "hover:opacity-80"
      } ${isTracked ? "text-amber-400" : "text-muted"} ${className}`}
    >
      {isTracked ? "★" : "☆"}
    </button>
  );
}
