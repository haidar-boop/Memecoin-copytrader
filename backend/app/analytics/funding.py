"""Wallet funding-source tracing for the vetting pipeline.

Sybil rings and insider wallets are usually funded directly by another
private wallet (often the ring's treasury); organic traders overwhelmingly
arrive via a centralized-exchange withdrawal. Tracing where a wallet's very
first SOL came from is therefore a cheap, high-signal vetting input.

The trace is deliberately conservative: whenever we cannot see the true
beginning of the wallet's history (page cap hit, RPC failure, no incoming
native transfer among the earliest transactions), the result is marked
``inconclusive`` rather than guessed — downstream scoring must treat an
inconclusive trace as "no evidence", never as "clear".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.logging_config import get_logger

log = get_logger(__name__)

# Curated starter list of well-known mainnet exchange hot wallets. This is a
# heuristic, not a registry: exchanges rotate hot wallets, and absence from
# this set only means "not a *known* CEX", never "not a CEX". Extend as new
# hot wallets are identified.
KNOWN_CEX_HOT_WALLETS: frozenset[str] = frozenset(
    {
        # Binance
        "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
        "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
        # Coinbase
        "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
        "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm",
        # Bybit
        "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2",
        # OKX
        "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD",
    }
)

PAGE_SIZE = 1000
# How many of the earliest transactions to inspect for the funding transfer.
# The first incoming SOL is almost always in the very first transaction; a
# small window tolerates a failed tx or an ATA-creation landing first.
EARLIEST_TX_WINDOW = 3


@dataclass
class FundingInfo:
    """Outcome of a funding trace.

    ``kind`` is "cex" | "wallet" | "unknown". ``inconclusive`` means the
    trace could not establish anything (deep history, no visible incoming
    transfer, RPC failure) — callers must not score it as evidence.
    """

    funder: str | None
    kind: str = "unknown"
    inconclusive: bool = False
    note: str = ""


def _account_keys(tx: dict) -> list[str]:
    """Account keys in on-chain order, tolerating both jsonParsed dicts
    ({"pubkey": ...}) and plain string keys, plus lookup-table addresses."""
    message = tx.get("transaction", {}).get("message", {})
    keys: list[str] = []
    for entry in message.get("accountKeys", []) or []:
        keys.append(entry["pubkey"] if isinstance(entry, dict) else str(entry))
    loaded = tx.get("meta", {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable", []) or [])
    keys.extend(loaded.get("readonly", []) or [])
    return keys


def _incoming_funder(tx: dict, address: str) -> str | None:
    """The account that funded ``address`` with native SOL in this tx.

    Ground truth is lamport balance deltas (same shape the DEX parsers use):
    if ``address`` ended up with more native SOL, the account with the
    largest decrease paid for it. Instruction decoding is deliberately
    avoided — deltas are immune to how the transfer was routed.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    keys = _account_keys(tx)
    bound = min(len(pre), len(post), len(keys))

    deltas: dict[str, int] = {}
    for i in range(bound):
        deltas[keys[i]] = deltas.get(keys[i], 0) + (post[i] - pre[i])

    if deltas.get(address, 0) <= 0:
        return None
    senders = {key: delta for key, delta in deltas.items() if key != address and delta < 0}
    if not senders:
        return None
    return min(senders, key=lambda key: senders[key])


async def trace_funder(rpc, settings, address: str) -> FundingInfo:
    """Trace which account first funded ``address`` with native SOL.

    Pages signature history newest-first up to ``vetting_funding_max_pages``
    pages; if history is deeper than the cap we never reach the wallet's
    beginning, so the trace is inconclusive by construction. Otherwise the
    earliest few transactions are inspected oldest-first for the first
    incoming native SOL transfer. All RPC goes through the normal daily
    budget (vetting is a background job; it can wait).
    """
    signatures: list[dict] = []
    last_page_len = 0
    before: str | None = None
    try:
        for _ in range(max(1, int(settings.vetting_funding_max_pages))):
            page = await rpc.get_signatures_for_address(
                address, limit=PAGE_SIZE, before=before, budget_exempt=None
            )
            last_page_len = len(page)
            if not page:
                break
            signatures.extend(page)
            before = page[-1].get("signature")
            if len(page) < PAGE_SIZE:
                break

        if last_page_len == PAGE_SIZE:
            return FundingInfo(
                None, "unknown", inconclusive=True, note="history deeper than page cap"
            )

        # History is newest-first, so the wallet's earliest activity sits at
        # the tail; inspect it oldest-first.
        earliest = [
            entry.get("signature") for entry in reversed(signatures[-EARLIEST_TX_WINDOW:])
        ]
        for signature in earliest:
            if not signature:
                continue
            tx = await rpc.get_transaction(signature, budget_exempt=None)
            if tx is None:
                continue
            funder = _incoming_funder(tx, address)
            if funder:
                kind = "cex" if funder in KNOWN_CEX_HOT_WALLETS else "wallet"
                return FundingInfo(funder, kind)
    except Exception as exc:  # noqa: BLE001 - vetting must degrade, not crash
        log.warning("funding_trace_rpc_error", address=address, error=str(exc))
        return FundingInfo(
            None, "unknown", inconclusive=True, note=f"rpc error: {exc}"
        )

    return FundingInfo(
        None,
        "unknown",
        inconclusive=True,
        note="no incoming native SOL transfer in earliest transactions",
    )
