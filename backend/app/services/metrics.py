"""Prometheus metrics shared across the API and workers.

Each worker process exposes these on its own HTTP port via
``start_metrics_server``; the API serves them at /metrics.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

WS_MESSAGES = Counter("ws_messages_total", "WebSocket log notifications received", ["program"])
WS_RECONNECTS = Counter("ws_reconnects_total", "WebSocket reconnect attempts")
SIGNATURES_ENQUEUED = Counter("signatures_enqueued_total", "Signatures pushed to ingest stream")
SIGNATURES_DEDUPED = Counter("signatures_deduped_total", "Duplicate signatures dropped")

TX_FETCHED = Counter("tx_fetched_total", "getTransaction results", ["status"])
TRADES_WRITTEN = Counter("trades_written_total", "Trade rows inserted", ["dex"])
PARSE_FAILURES = Counter("parse_failures_total", "Transactions that failed to parse")
DB_WRITE_ERRORS = Counter("db_write_errors_total", "Database write errors")

RPC_REQUESTS = Counter("rpc_requests_total", "Solana RPC requests", ["method", "status"])
RPC_LATENCY = Histogram("rpc_latency_seconds", "Solana RPC latency", ["method"])

ENRICH_RUNS = Counter("enrichment_runs_total", "Enrichment job runs", ["job", "status"])

QUEUE_DEPTH = Gauge("ingest_queue_depth", "Pending entries in the ingest stream")
LAST_SLOT = Gauge("last_seen_slot", "Highest slot observed by the listener")


def start_metrics_server(port: int) -> None:
    start_http_server(port)
