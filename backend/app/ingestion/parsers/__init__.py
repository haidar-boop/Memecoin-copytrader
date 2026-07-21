"""Parser registry.

Venue parsers are discovered by module/class name from ``_PARSER_SPECS``.
Import failures are tolerated (with a warning) so venues can be developed and
deployed independently; a missing parser must never take down ingestion.

``parse_transaction`` is the single entry point used by the ingest writer:
1. Every matching venue parser runs (priority = spec order).
2. Aggregator (fallback_only) parsers run only if no venue parser emitted.
3. Events are tagged with aggregator="jupiter" when a Jupiter program is
   present, deduped by (wallet, token_mint, side, quote_mint) — multiple
   venue parsers inferring from the same balance deltas would otherwise
   duplicate — and given a stable event_index.
"""

from __future__ import annotations

import importlib
from functools import lru_cache

from app.ingestion.events import SwapEvent
from app.ingestion.parsers import util
from app.ingestion.programs import JUPITER_PROGRAM_IDS
from app.logging_config import get_logger

log = get_logger(__name__)

# (module in app.ingestion.parsers, class name). Order = dedup priority.
_PARSER_SPECS: list[tuple[str, str]] = [
    ("pumpfun", "PumpFunParser"),
    ("raydium", "RaydiumParser"),
    ("orca", "OrcaWhirlpoolParser"),
    ("jupiter", "JupiterParser"),
]


@lru_cache(maxsize=1)
def get_parsers() -> tuple:
    parsers = []
    for module_name, class_name in _PARSER_SPECS:
        try:
            module = importlib.import_module(f"app.ingestion.parsers.{module_name}")
            parsers.append(getattr(module, class_name)())
        except (ImportError, AttributeError) as exc:
            log.warning("parser_unavailable", parser=module_name, error=str(exc))
    return tuple(parsers)


def parse_transaction(tx: dict) -> list[SwapEvent]:
    if not util.is_success(tx):
        return []

    parsers = get_parsers()
    events: list[SwapEvent] = []
    for parser in parsers:
        if parser.fallback_only or not parser.matches(tx):
            continue
        try:
            events.extend(parser.parse(tx))
        except Exception:
            log.exception("parser_error", parser=parser.dex.value)
    if not events:
        for parser in parsers:
            if not parser.fallback_only or not parser.matches(tx):
                continue
            try:
                events.extend(parser.parse(tx))
            except Exception:
                log.exception("parser_error", parser=parser.dex.value)

    if JUPITER_PROGRAM_IDS & util.program_ids(tx):
        for event in events:
            event.aggregator = "jupiter"

    deduped: list[SwapEvent] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        key = (event.wallet, event.token_mint, event.side.value, event.quote_mint)
        if key in seen:
            continue
        seen.add(key)
        event.event_index = len(deduped)
        deduped.append(event)
    return deduped
