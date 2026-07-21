"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { clearToken, isAuthed } from "@/lib/auth";
import { NotificationBell } from "./NotificationBell";

const NAV: Array<{ href: string; label: string; icon: string }> = [
  { href: "/", label: "Overview", icon: "▚" },
  { href: "/wallets", label: "Wallets", icon: "◈" },
  { href: "/strategies", label: "Strategies", icon: "❖" },
  { href: "/trades", label: "Live Trades", icon: "⚡" },
  { href: "/copytrading", label: "Copy Trading", icon: "⇄" },
  { href: "/reports", label: "Reports", icon: "▤" },
  { href: "/health", label: "Health", icon: "♥" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [authed, setAuthed] = useState<boolean | null>(null);

  const isLogin = pathname === "/login";

  useEffect(() => {
    const ok = isAuthed();
    setAuthed(ok);
    if (!ok && !isLogin) {
      router.replace("/login");
    }
  }, [pathname, isLogin, router]);

  if (isLogin) {
    return <>{children}</>;
  }

  if (authed === null) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted">
        Loading…
      </div>
    );
  }

  if (!authed) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted">
        Redirecting to login…
      </div>
    );
  }

  const logout = () => {
    clearToken();
    router.replace("/login");
  };

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-edge bg-panel">
        <div className="px-4 py-5 text-lg font-bold tracking-tight">
          <span className="text-accent">◎</span> Copytrader
        </div>
        <nav className="flex-1 px-2">
          {NAV.map((n) => {
            const active =
              n.href === "/"
                ? pathname === "/"
                : pathname.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className={`mb-1 flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                  active
                    ? "bg-accent/15 text-accent"
                    : "text-muted hover:bg-panel2 hover:text-white"
                }`}
              >
                <span className="w-4 text-center">{n.icon}</span>
                {n.label}
              </Link>
            );
          })}
        </nav>
        <button
          onClick={logout}
          className="m-2 rounded-lg px-3 py-2 text-left text-sm text-muted hover:bg-panel2 hover:text-white"
        >
          ⎋ Log out
        </button>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-edge bg-panel/60 px-6 py-3">
          <div className="text-sm text-muted">
            Solana Memecoin Copy-Trading Intelligence
          </div>
          <NotificationBell />
        </header>
        <main className="min-w-0 flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
