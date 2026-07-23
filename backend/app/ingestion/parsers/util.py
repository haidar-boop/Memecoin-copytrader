"""Shared transaction-inspection helpers used by every DEX parser.

The ground truth for swap amounts is the transaction's balance deltas
(pre/post token balances plus native lamport balances), not per-DEX
instruction decoding: balance deltas are identical across venues and immune
to instruction-layout changes. DEX parsers contribute venue attribution and
pool identification on top of these helpers.

Known accepted noise: when a swap creates or closes token accounts in the
same transaction, rent (~0.00204 SOL per account) is folded into the native
SOL delta. At memecoin position sizes this is dust; Phase 2+ can correct it
using parsed system-program transfers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.programs import QUOTE_MINTS, STABLE_MINTS, WSOL_MINT

LAMPORTS_PER_SOL = Decimal(10) ** 9
# Ignore native flows below this (rent churn, tips) when picking the quote leg.
MIN_SOL_FLOW = Decimal("0.0005")


def account_keys(tx: dict) -> list[str]:
    """All account keys in on-chain order: static, then loaded (writable, readonly)."""
    message = tx.get("transaction", {}).get("message", {})
    keys: list[str] = []
    for entry in message.get("accountKeys", []) or []:
        keys.append(entry["pubkey"] if isinstance(entry, dict) else str(entry))
    loaded = tx.get("meta", {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable", []) or [])
    keys.extend(loaded.get("readonly", []) or [])
    return keys


def fee_payer(tx: dict) -> str:
    message = tx.get("transaction", {}).get("message", {})
    for entry in message.get("accountKeys", []) or []:
        if isinstance(entry, dict) and entry.get("signer"):
            return entry["pubkey"]
    keys = account_keys(tx)
    return keys[0] if keys else ""


def log_messages(tx: dict) -> list[str]:
    return list(tx.get("meta", {}).get("logMessages") or [])


def is_success(tx: dict) -> bool:
    return tx.get("meta", {}).get("err") is None


def block_time(tx: dict) -> datetime:
    ts = tx.get("blockTime")
    if ts is None:
        return datetime.now(tz=UTC)
    return datetime.fromtimestamp(int(ts), tz=UTC)


def program_ids(tx: dict) -> set[str]:
    """Program ids invoked at the top level or via inner instructions (CPI)."""
    pids: set[str] = set()
    message = tx.get("transaction", {}).get("message", {})
    for ix in message.get("instructions", []) or []:
        if pid := ix.get("programId"):
            pids.add(str(pid))
    for group in tx.get("meta", {}).get("innerInstructions", []) or []:
        for ix in group.get("instructions", []) or []:
            if pid := ix.get("programId"):
                pids.add(str(pid))
    return pids


def token_balance_deltas(tx: dict) -> dict[tuple[str, str], Decimal]:
    """Net UI-amount change per (owner, mint), summed across token accounts."""
    meta = tx.get("meta", {})
    deltas: dict[tuple[str, str], Decimal] = {}

    def accumulate(entries: list | None, sign: int) -> None:
        for entry in entries or []:
            owner, mint = entry.get("owner"), entry.get("mint")
            ui = entry.get("uiTokenAmount") or {}
            raw_amount = ui.get("amount")
            if not owner or not mint or raw_amount is None:
                continue
            scale = Decimal(10) ** int(ui.get("decimals", 0))
            key = (owner, mint)
            deltas[key] = deltas.get(key, Decimal(0)) + sign * (Decimal(raw_amount) / scale)

    accumulate(meta.get("postTokenBalances"), 1)
    accumulate(meta.get("preTokenBalances"), -1)
    return {k: v for k, v in deltas.items() if v != 0}


# Jito block-engine tip accounts (static, mainnet). Trading bots attach a
# tip transfer alongside the swap; those lamports leave the fee payer but
# are not part of the trade, so they must be added back like the tx fee.
JITO_TIP_ACCOUNTS = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)


def native_sol_delta(tx: dict, owner: str) -> Decimal:
    """Net native SOL change for ``owner`` in SOL, with the tx fee and any
    Jito tips added back for the fee payer so the delta reflects the trade
    itself rather than what the trader paid to land it."""
    meta = tx.get("meta", {})
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    keys = account_keys(tx)
    bound = min(len(pre), len(post))
    lamports = sum(
        post[i] - pre[i] for i, key in enumerate(keys) if key == owner and i < bound
    )
    if owner == fee_payer(tx):
        lamports += int(meta.get("fee", 0))
        for i, key in enumerate(keys):
            if key in JITO_TIP_ACCOUNTS and i < bound and post[i] > pre[i]:
                lamports += post[i] - pre[i]
    return Decimal(lamports) / LAMPORTS_PER_SOL


def infer_swap_events(
    tx: dict,
    owner: str,
    dex: Dex,
    program_id: str | None = None,
    pool_address: str | None = None,
) -> list[SwapEvent]:
    """Derive normalized swap events for ``owner`` from balance deltas.

    Handles the three shapes that matter on memecoin venues:
    - token <-> WSOL (wrapped SOL visible in token balances)
    - token <-> native SOL (e.g. pump.fun bonding curve; WSOL account is
      created and closed inside the tx, so only lamport deltas show it)
    - token <-> stablecoin, and token <-> token (two events emitted)
    """
    if not is_success(tx):
        return []

    deltas = token_balance_deltas(tx)
    non_quote = {
        mint: delta for (o, mint), delta in deltas.items() if o == owner and mint not in QUOTE_MINTS
    }
    wsol_delta = deltas.get((owner, WSOL_MINT), Decimal(0))
    stable_deltas = {
        mint: deltas.get((owner, mint), Decimal(0))
        for mint in STABLE_MINTS
        if deltas.get((owner, mint))
    }
    native = native_sol_delta(tx, owner)

    common = {
        "signature": (tx.get("transaction", {}).get("signatures") or [""])[0],
        "slot": int(tx.get("slot", 0)),
        "block_time": block_time(tx),
        "wallet": owner,
        "dex": dex,
        "program_id": program_id,
        "pool_address": pool_address,
    }

    def build(
        token_mint: str,
        token_delta: Decimal,
        quote_mint: str,
        quote_amount: Decimal,
        raw: dict | None = None,
    ) -> SwapEvent:
        token_amount = abs(token_delta)
        price = (quote_amount / token_amount) if token_amount and quote_amount else None
        return SwapEvent(
            side=Side.BUY if token_delta > 0 else Side.SELL,
            token_mint=token_mint,
            quote_mint=quote_mint,
            token_amount=token_amount,
            quote_amount=quote_amount,
            price_quote_per_token=price,
            raw=raw,
            **common,
        )

    if len(non_quote) == 1:
        (mint, delta), = non_quote.items()
        # Pick the counter-leg (the quote the token traded against) as the
        # LARGEST opposite-signed flow across BOTH the SOL and stable pools.
        # WSOL, native lamports, and a stablecoin can all move in one tx
        # (unwrap dust, ATA rent ~0.00204 SOL, route legs); comparing only SOL
        # first let rent-sized SOL preempt a real USDC/USDT leg (a 100-USDC buy
        # recorded as a 0.002-SOL buy, corrupting price/PnL/scores). Taking the
        # max magnitude across both pools makes rent lose to the real quote and
        # still makes a genuine 1.5 SOL leg win over 0.001 SOL of dust.
        native_flow = native if abs(native) >= MIN_SOL_FLOW else Decimal(0)
        candidates: list[tuple[str, Decimal]] = [
            (WSOL_MINT, flow)
            for flow in (wsol_delta, native_flow)
            if flow != 0 and (flow > 0) != (delta > 0)
        ]
        candidates += [
            (stable_mint, sdelta)
            for stable_mint, sdelta in stable_deltas.items()
            if (sdelta > 0) != (delta > 0)
        ]
        if candidates:
            quote_mint, flow = max(candidates, key=lambda c: abs(c[1]))
            return [build(mint, delta, quote_mint, abs(flow))]
        # No identifiable counter-leg (LP ops, transfers): not a swap.
        return []

    if len(non_quote) == 2:
        (mint_a, delta_a), (mint_b, delta_b) = sorted(non_quote.items())
        if (delta_a > 0) == (delta_b > 0):
            return []
        raw = {"token_to_token": True}
        return [
            build(mint_a, delta_a, mint_b, abs(delta_b), raw=raw),
            build(mint_b, delta_b, mint_a, abs(delta_a), raw=raw),
        ]

    return []
