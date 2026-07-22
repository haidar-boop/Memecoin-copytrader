"""Tests for the wallet funding-source trace used by vetting."""

from __future__ import annotations

from app.analytics.funding import KNOWN_CEX_HOT_WALLETS, FundingInfo, trace_funder
from app.config import Settings

ADDRESS = "FreshWa11et11111111111111111111111111111111"
PRIVATE_FUNDER = "PrivateFunder111111111111111111111111111111"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


def make_settings(max_pages: int = 3) -> Settings:
    return Settings(vetting_funding_max_pages=max_pages)


def sig_entry(signature: str) -> dict:
    return {"signature": signature, "err": None}


def funding_tx(
    recipient: str,
    funder: str,
    lamports: int = 1_000_000_000,
    plain_keys: bool = False,
) -> dict:
    """Minimal jsonParsed getTransaction shape: funder sends SOL to recipient."""
    keys: list = [funder, recipient, "11111111111111111111111111111111"]
    if not plain_keys:
        keys = [
            {"pubkey": funder, "signer": True, "writable": True},
            {"pubkey": recipient, "signer": False, "writable": True},
            {"pubkey": "11111111111111111111111111111111", "signer": False, "writable": False},
        ]
    return {
        "blockTime": 1753000000,
        "slot": 1,
        "transaction": {"message": {"accountKeys": keys}, "signatures": ["fund-sig"]},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [5_000_000_000, 0, 1],
            "postBalances": [5_000_000_000 - lamports - 5000, lamports, 1],
        },
    }


def no_transfer_tx(address: str) -> dict:
    """A tx where ``address`` received nothing (e.g. it paid a fee)."""
    return {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": address, "signer": True, "writable": True}]
            },
            "signatures": ["noop-sig"],
        },
        "meta": {"err": None, "fee": 5000, "preBalances": [10_000], "postBalances": [5_000]},
    }


class CannedRpc:
    """Canned-response RPC double for signature history + transactions."""

    def __init__(
        self,
        pages: list[list[dict]],
        txs: dict[str, dict] | None = None,
        fail: bool = False,
    ) -> None:
        self.pages = list(pages)
        self.txs = txs or {}
        self.fail = fail
        self.before_args: list[str | None] = []
        self.fetched: list[str] = []

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 1000,
        before: str | None = None,
        budget_exempt: bool | None = None,
    ) -> list[dict]:
        if self.fail:
            raise RuntimeError("rpc down")
        self.before_args.append(before)
        return self.pages.pop(0) if self.pages else []

    async def get_transaction(
        self, signature: str, budget_exempt: bool | None = None
    ) -> dict | None:
        if self.fail:
            raise RuntimeError("rpc down")
        self.fetched.append(signature)
        return self.txs.get(signature)


async def test_private_wallet_funder() -> None:
    rpc = CannedRpc(
        pages=[[sig_entry("later-sig"), sig_entry("fund-sig")]],
        txs={"fund-sig": funding_tx(ADDRESS, PRIVATE_FUNDER)},
    )
    info = await trace_funder(rpc, make_settings(), ADDRESS)
    assert info == FundingInfo(PRIVATE_FUNDER, "wallet")
    # Oldest transaction inspected first.
    assert rpc.fetched[0] == "fund-sig"


async def test_plain_string_account_keys() -> None:
    rpc = CannedRpc(
        pages=[[sig_entry("fund-sig")]],
        txs={"fund-sig": funding_tx(ADDRESS, PRIVATE_FUNDER, plain_keys=True)},
    )
    info = await trace_funder(rpc, make_settings(), ADDRESS)
    assert info.funder == PRIVATE_FUNDER
    assert info.kind == "wallet"


async def test_cex_funder() -> None:
    assert BINANCE in KNOWN_CEX_HOT_WALLETS
    rpc = CannedRpc(
        pages=[[sig_entry("fund-sig")]],
        txs={"fund-sig": funding_tx(ADDRESS, BINANCE)},
    )
    info = await trace_funder(rpc, make_settings(), ADDRESS)
    assert info.funder == BINANCE
    assert info.kind == "cex"
    assert info.inconclusive is False


async def test_history_deeper_than_page_cap_is_inconclusive() -> None:
    full_page = [sig_entry(f"s{i}") for i in range(1000)]
    rpc = CannedRpc(pages=[full_page, full_page])
    info = await trace_funder(rpc, make_settings(max_pages=2), ADDRESS)
    assert info.funder is None
    assert info.inconclusive is True
    assert "page cap" in info.note
    # Never fetched any transaction: the wallet's beginning was unreachable.
    assert rpc.fetched == []
    # Paged with before=<last signature of previous page>.
    assert rpc.before_args == [None, "s999"]


async def test_no_incoming_transfer_is_inconclusive() -> None:
    rpc = CannedRpc(
        pages=[[sig_entry("noop-sig")]],
        txs={"noop-sig": no_transfer_tx(ADDRESS)},
    )
    info = await trace_funder(rpc, make_settings(), ADDRESS)
    assert info.funder is None
    assert info.kind == "unknown"
    assert info.inconclusive is True


async def test_rpc_error_is_inconclusive() -> None:
    rpc = CannedRpc(pages=[], fail=True)
    info = await trace_funder(rpc, make_settings(), ADDRESS)
    assert info.funder is None
    assert info.inconclusive is True
    assert "rpc down" in info.note
