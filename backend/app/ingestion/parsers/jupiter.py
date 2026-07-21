"""Jupiter aggregator fallback adapter (router v4 + v6).

Jupiter is a router, not a venue: its ``route`` instruction CPIs into
whichever DEX pools offer the best price. When one of those pools belongs to
a venue with its own adapter, that adapter claims the transaction and the
registry merely tags the event with ``aggregator="jupiter"``. This parser is
therefore ``fallback_only``: the registry runs it only when *no* venue
adapter emitted events — i.e. the route went through venues we do not (yet)
support. Instead of dropping such trades we still capture them, attributed
to ``Dex.JUPITER``.

Amounts, side and price come from ``util.infer_swap_events`` (balance-delta
ground truth), which also collapses multi-hop routes naturally: intermediate
legs net to zero in the trader's balance deltas, leaving only the entry and
exit legs. ``pool_address`` is always ``None`` — a Jupiter route may touch
several pools, and naming one of them would be a guess.
"""

from __future__ import annotations

from app.ingestion.events import Dex, SwapEvent
from app.ingestion.parsers import util
from app.ingestion.parsers.base import BaseDexParser
from app.ingestion.programs import JUPITER_V4, JUPITER_V6
from app.logging_config import get_logger

log = get_logger(__name__)

# Newest router first: if several Jupiter versions somehow appear in one
# transaction, attribute the event to the newest one deterministically.
_PROGRAM_PRIORITY: tuple[str, ...] = (JUPITER_V6, JUPITER_V4)


class JupiterParser(BaseDexParser):
    """Aggregator fallback: ``Dex.JUPITER`` events for unsupported routes."""

    dex = Dex.JUPITER
    program_ids = frozenset({JUPITER_V6, JUPITER_V4})
    fallback_only = True

    def parse(self, tx: dict) -> list[SwapEvent]:
        program_id = self._routed_program_id(tx)
        if program_id is None:
            return []
        wallet = util.fee_payer(tx)
        if not wallet:
            log.debug(
                "jupiter_no_fee_payer",
                signature=(tx.get("transaction", {}).get("signatures") or [""])[0],
            )
            return []
        return util.infer_swap_events(
            tx,
            wallet,
            Dex.JUPITER,
            program_id=program_id,
            pool_address=None,
        )

    def _routed_program_id(self, tx: dict) -> str | None:
        """The Jupiter router program invoked by this transaction, if any."""
        present = self.program_ids & util.program_ids(tx)
        return next((pid for pid in _PROGRAM_PRIORITY if pid in present), None)
