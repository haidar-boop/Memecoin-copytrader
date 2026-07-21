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
