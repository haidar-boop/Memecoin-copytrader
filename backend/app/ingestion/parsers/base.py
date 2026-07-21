"""Parser interface every DEX adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.ingestion.events import Dex, SwapEvent
from app.ingestion.parsers import util


class BaseDexParser(ABC):
    """One adapter per venue.

    Contract:
    - ``program_ids`` is the set of on-chain program ids the venue uses.
    - ``matches`` is cheap: presence of any program id in the transaction.
    - ``parse`` returns normalized SwapEvents (usually via
      ``util.infer_swap_events``) and MUST NOT raise on weird-but-valid
      transactions — return [] instead.
    - Parsers marked ``fallback_only`` (aggregators) run only when no
      venue-specific parser produced events for the transaction.
    """

    dex: Dex
    program_ids: frozenset[str]
    fallback_only: bool = False

    def matches(self, tx: dict) -> bool:
        return util.is_success(tx) and bool(self.program_ids & util.program_ids(tx))

    def matched_program_id(self, tx: dict) -> str | None:
        present = self.program_ids & util.program_ids(tx)
        return next(iter(present), None)

    @abstractmethod
    def parse(self, tx: dict) -> list[SwapEvent]: ...
