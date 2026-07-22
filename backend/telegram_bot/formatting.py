"""Pure string builders for Telegram messages.

Every function here is deterministic and free of I/O so it can be unit-tested
exhaustively. Text is plain (no parse_mode assumed); emojis prefix each line to
make severities and kinds scannable in a chat client.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Emoji per notification severity.
SEVERITY_EMOJI: dict[str, str] = {
    "info": "ℹ️",  # ℹ️
    "warning": "⚠️",  # ⚠️
    "critical": "\U0001f6a8",  # 🚨
}
DEFAULT_SEVERITY_EMOJI = "ℹ️"

# Emoji per notification kind (see app.services.notifications.NOTIFICATION_KINDS).
KIND_EMOJI: dict[str, str] = {
    "new_high_confidence_wallet": "⭐",  # ⭐
    "copied_buy": "\U0001f7e2",  # 🟢
    "copied_sell": "\U0001f534",  # 🔴
    "stop_loss": "\U0001f6d1",  # 🛑
    "take_profit": "\U0001f4b0",  # 💰
    "daily_summary": "\U0001f4c5",  # 📅
    "weekly_report": "\U0001f4c8",  # 📈
    "confidence_change": "\U0001f504",  # 🔄
    "large_market_move": "\U0001f30a",  # 🌊
    "system_error": "❌",  # ❌
    "emergency_stop": "\U0001f6a8",  # 🚨
}
DEFAULT_KIND_EMOJI = "\U0001f514"  # 🔔


def severity_emoji(severity: str | None) -> str:
    return SEVERITY_EMOJI.get(severity or "", DEFAULT_SEVERITY_EMOJI)


def kind_emoji(kind: str | None) -> str:
    return KIND_EMOJI.get(kind or "", DEFAULT_KIND_EMOJI)


def notification_to_text(notification: dict[str, Any]) -> str:
    """Render a notification dict (see :class:`Notification`) to chat text."""
    kind = str(notification.get("kind", ""))
    severity = str(notification.get("severity", "info"))
    title = str(notification.get("title", "") or kind or "Notification")
    body = str(notification.get("body", "") or "")
    ts = notification.get("ts")

    header = f"{severity_emoji(severity)} {kind_emoji(kind)} {title}"
    lines = [header]
    if body:
        lines.append(body)
    if ts:
        lines.append(f"\U0001f552 {ts}")  # 🕒
    return "\n".join(lines)


def _fmt_sol(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def format_status(status: dict[str, Any]) -> str:
    """Render SafetyGuard.status() output."""
    stop = status.get("emergency_stop")
    if stop:
        state_line = f"\U0001f6a8 EMERGENCY STOP: {stop}"
    else:
        state_line = "✅ Running (no emergency stop)"

    enabled = status.get("enabled")
    mode = status.get("mode", "?")
    lines = [
        "\U0001f4ca Copy-trading status",
        state_line,
        f"Mode: {mode}  |  Enabled: {'yes' if enabled else 'no'}",
        f"Open positions: {status.get('open_positions', 0)}",
        f"Exposure: {_fmt_sol(status.get('exposure_sol', 0))} SOL",
        f"Daily realized PnL: {_fmt_sol(status.get('daily_realized_pnl_sol', 0))} SOL",
    ]
    return "\n".join(lines)


def format_portfolio(positions: Iterable[dict[str, Any]], realized_pnl: Any) -> str:
    """Render open positions plus a realized-PnL footer."""
    positions = list(positions)
    lines = ["\U0001f4bc Portfolio"]
    if not positions:
        lines.append("No open positions.")
    else:
        for pos in positions:
            token = pos.get("token_mint") or pos.get("token_id") or "?"
            spent = _fmt_sol(pos.get("spent_sol", 0))
            sold = _fmt_sol(pos.get("sold_sol", 0))
            mode = pos.get("mode", "")
            lines.append(f"• {token} [{mode}]  spent {spent} / sold {sold} SOL")
    lines.append(f"Realized PnL: {_fmt_sol(realized_pnl)} SOL")
    return "\n".join(lines)


def format_report(report: dict[str, Any]) -> str:
    """Render a Report row (kind/summary/markdown/window)."""
    kind = str(report.get("kind", "report"))
    lines = [f"\U0001f4c4 {kind.replace('_', ' ').title()}"]
    generated = report.get("generated_at")
    if generated:
        lines.append(f"Generated: {generated}")
    window = None
    start, end = report.get("window_start"), report.get("window_end")
    if start and end:
        window = f"Window: {start} → {end}"
    if window:
        lines.append(window)

    summary = report.get("summary")
    markdown = report.get("markdown")
    if summary:
        lines.append("")
        lines.append(str(summary))
    elif markdown:
        lines.append("")
        lines.append(str(markdown))
    else:
        lines.append("(no summary available)")
    return "\n".join(lines)


def _short(addr: Any, keep: int = 4) -> str:
    s = str(addr or "?")
    return s if len(s) <= keep * 2 + 1 else f"{s[:keep]}…{s[-keep:]}"


def format_health(health: dict[str, Any]) -> str:
    """Render the bot-side system health summary."""

    def ok(flag: bool) -> str:
        return "✅" if flag else "\U0001f6a8"

    lines = [
        "\U0001fa7a System health",
        f"{ok(health.get('db_ok', False))} Database",
        f"{ok(health.get('redis_ok', False))} Redis",
    ]
    age = health.get("last_trade_age_minutes")
    if age is None:
        lines.append("\U0001f6a8 Ingestion: no trades recorded yet")
    else:
        flag = ok(age <= 30)
        lines.append(f"{flag} Ingestion: last trade {age:.0f} min ago")
    depth = health.get("queue_depth")
    if depth is not None:
        lines.append(f"Queue depth: {depth:,}")
    used, limit = health.get("credits_used"), health.get("credits_limit")
    if limit:
        pct = 100.0 * used / limit if used is not None else 0.0
        flag = ok(used is None or used <= limit)
        lines.append(f"{flag} RPC credits today: {used or 0:,} / {limit:,} ({pct:.0f}%)")
    elif used is not None:
        lines.append(f"RPC credits today: {used:,} (no cap)")
    p_used, p_limit = health.get("priority_credits_used"), health.get(
        "priority_credits_limit"
    )
    if p_limit:
        p_pct = 100.0 * p_used / p_limit if p_used is not None else 0.0
        p_flag = ok(p_used is None or p_used <= p_limit)
        lines.append(
            f"{p_flag} Priority-lane credits: {p_used or 0:,} / {p_limit:,} ({p_pct:.0f}%)"
        )
    frozen = health.get("rpc_frozen")
    if frozen:
        lines.append(f"\U0001f9ca RPC FROZEN ({frozen}) — credit spend is zero")
    stop = health.get("emergency_stop")
    lines.append(
        f"\U0001f6a8 EMERGENCY STOP: {stop}" if stop else "✅ No emergency stop"
    )
    return "\n".join(lines)


def format_top_wallets(rows: Iterable[dict[str, Any]]) -> str:
    """Render the top tracked wallets by confidence."""
    rows = list(rows)
    lines = ["\U0001f3c6 Top wallets by confidence"]
    if not rows:
        lines.append("No scored wallets yet — analytics needs more history.")
    for i, row in enumerate(rows, 1):
        conf = row.get("confidence_score")
        pnl = row.get("total_pnl_sol")
        win = row.get("win_rate")
        win_txt = f", win {float(win) * 100:.0f}%" if win is not None else ""
        # Suspicious wallets stay listed (hiding them would silently shrink
        # the leaderboard) but carry an explicit flag.
        flag = " ⚠️ FLAGGED" if row.get("vetting_verdict") == "suspicious" else ""
        lines.append(
            f"{i}. {_short(row.get('address'))} — conf {_fmt_sol(conf)}"
            f", PnL {_fmt_sol(pnl)} SOL{win_txt}{flag}"
        )
    return "\n".join(lines)


def format_recent_trades(rows: Iterable[dict[str, Any]]) -> str:
    """Render the most recent observed trades, newest first."""
    rows = list(rows)
    lines = ["⚡ Recent trades"]
    if not rows:
        lines.append("Nothing observed yet.")
    for row in rows:
        side = str(row.get("side", "?")).upper()
        arrow = "\U0001f7e2" if side == "BUY" else "\U0001f534"
        age = row.get("age_minutes")
        age_txt = f" ({age:.0f}m ago)" if age is not None else ""
        lines.append(
            f"{arrow} {side} {_short(row.get('token_mint'))} "
            f"{_fmt_sol(row.get('quote_amount'))} SOL by {_short(row.get('wallet'))}"
            f"{age_txt}"
        )
    return "\n".join(lines)
