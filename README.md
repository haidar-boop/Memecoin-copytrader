# Memecoin Copytrader — Phase 1: Intelligence Engine

AI-powered Solana memecoin copy-trading platform. **Phase 1 does not trade.**
It continuously watches Solana DEX activity and builds an append-only learning
database of wallets, tokens, trades, market conditions, and outcomes — the
foundation the later analysis, decision, and execution phases learn from.

## Architecture

```
 Solana WS (logsSubscribe: Raydium, Pump.fun/PumpSwap, Orca, Jupiter)
        │ signatures
        ▼
  listener ──▶ Redis stream (dedup, buffer) ──▶ ingest-writer ──▶ PostgreSQL/TimescaleDB
                                                    │  getTransaction → parser registry
                                                    │  (balance-delta ground truth)
                                                    └─▶ Redis pub/sub events:trades
  enrichment ──▶ token metadata / liquidity / holders / wallet balances
                 + token/market snapshots (append-only time series)
  api (FastAPI) ──▶ wallets, tokens, trades, positions, snapshots, stats,
                    /health, /metrics (Prometheus)
```

Every component is a separate process; the parser layer is modular (one file
per venue behind a common `SwapEvent` interface), so adding a DEX is a single
adapter.

### Data model highlights

- **Append-only**: `transactions`, `trades`, `failed_transactions` and all
  `*_snapshots` tables are TimescaleDB hypertables (1-day chunks) with BRIN
  indexes; rows are never updated. Everything is timestamped with both
  `block_time` and ingestion time. Failed transactions are stored too — they
  are signal.
- **Positions** are derived state (open/closed episodes per wallet+token) with
  realized PNL, ROI, and hold time in SOL terms; trades remain the immutable
  source of truth.
- **Snapshots** capture price, VWAP, volume (5m/1h/24h), liquidity, holder
  counts, market cap, and market-wide aggregates for later regime learning.

## Quickstart

```bash
cp .env.example .env      # set SOLANA_RPC_URL/SOLANA_WS_URL to a provider endpoint
make up                   # timescaledb + redis + migrate + api + workers
open http://localhost:8000/docs
curl localhost:8000/api/stats/ingestion
```

Public mainnet RPC rate-limits aggressively; for sustained ingestion use a
provider endpoint (Helius/Triton/QuickNode) in `.env`.

## Development

```bash
make install   # pip install -e backend[dev]
make test      # offline unit tests (SQLite, fixture transactions)
make lint
```

Migrations: `cd backend && alembic upgrade head` (Timescale features degrade
gracefully on plain PostgreSQL). New revisions: `make revision m="add xyz"`.

## Configuration

All knobs are environment variables parsed by `backend/app/config.py`
(pydantic-settings); `.env.example` documents the important ones, including
enabled venues (`ENABLED_DEXES`), rate limits, snapshot intervals, and the
optional heavy holder-count job (`HOLDERS_ENABLED`).

## Monitoring

- `GET /health/live`, `GET /health/ready` (DB + Redis checks)
- `GET /metrics` on the API; each worker exposes Prometheus metrics on
  `METRICS_PORT` (ingest lag, queue depth, RPC errors, reconnects,
  parse/write counters, enrichment runs)
- `GET /api/stats/ingestion` for table counts, queue depth, listener
  checkpoint

## Roadmap

- **Phase 2**: wallet analysis & strategy learning (metrics, confidence
  scores, strategy clustering, ML models, pattern discovery)
- **Phase 3**: decision engine & copy trading (evidence-gated, paper-trading
  default, strict safety limits)
- **Phase 4**: continuous self-evaluation & market regime learning
- **Phase 5**: production platform (Next.js dashboard, auth, Telegram,
  WebSocket feeds)

> Research/analytics software. Nothing here is financial advice; memecoin
> trading carries extreme risk.
