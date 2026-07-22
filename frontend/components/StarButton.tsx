"use client";

import React, { useEffect, useRef, useState } from "react";
import { trackWallet, untrackWallet } from "@/lib/api";

const ERROR_VISIBLE_MS = 6000;

/**
 * Star toggle for manually tracking a wallet. Optimistic: flips instantly,
 * reverts on failure. Errors render as visible inline text (auto-hiding),
 * not a hover tooltip — most operators are on touch devices.
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
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setIsTracked(tracked), [tracked]);
  useEffect(
    () => () => {
      if (errorTimer.current) clearTimeout(errorTimer.current);
    },
    [],
  );

  function showError(message: string) {
    setError(message);
    if (errorTimer.current) clearTimeout(errorTimer.current);
    errorTimer.current = setTimeout(() => setError(null), ERROR_VISIBLE_MS);
  }

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
      showError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className={`inline-flex items-center gap-1 ${className}`}>
      <button
        onClick={toggle}
        disabled={busy}
        aria-label={isTracked ? "Unstar wallet" : "Star wallet"}
        aria-pressed={isTracked}
        title={
          isTracked
            ? "Tracked — the copy engine follows this wallet. Tap to unstar."
            : "Star to have the copy engine follow this wallet."
        }
        className={`text-lg leading-none transition-opacity ${
          busy ? "opacity-40" : "hover:opacity-80"
        } ${isTracked ? "text-amber-400" : "text-muted"}`}
      >
        {isTracked ? "★" : "☆"}
      </button>
      {error && (
        <span role="alert" className="max-w-[10rem] truncate text-xs text-bad">
          {error}
        </span>
      )}
    </span>
  );
}
