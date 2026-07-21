# Memecoin Copytrader — Intelligence Engine & Analysis Brain

AI-powered Solana memecoin copy-trading platform. **Phases 1-2 do not trade.**
Phase 1 continuously watches Solana DEX activity and builds an append-only
learning database of wallets, tokens, trades, market conditions, and
outcomes. Phase 2 is the brain on top of it: wallet performance metrics,
evidence-gated confidence scores with full explanations, trading-style
recognition, calibrated ML models, and pattern discovery.

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

## Phase 2 — Wallet Analysis & Strategy Learning

The `analytics` worker runs four periodic jobs (intervals in `.env`):

- **wallet_stats** — per-wallet ROI, win rate, profit factor, max drawdown,
  hold times, position sizing, entry timing, consistency; every cycle also
  appends a `wallet_stats_snapshots` row so score evolution is itself
  training data. Confidence scores (0-100) come from
  `app/analytics/confidence.py`: Bayesian-shrunk components with a stored
  explanation payload — the API always answers *why* a wallet scores what it
  does.
- **strategy** — feature-based clustering (scikit-learn KMeans) into named
  styles (sniper, scalper, momentum, swing, holder, accumulator) plus
  per-style performance windows; falls back to rule-based labeling below
  `STRATEGY_MIN_WALLETS`.
- **patterns** — evidence-gated pattern rows (time-of-day effects, token
  lifecycle/entry timing, position-size effects, hold-time buckets, whale
  flows, volume trends), each with sample counts and baselines.
- **ml_retrain** — calibrated gradient-boosting models (`trade_profit`,
  `wallet_persistence`) with time-ordered walk-forward evaluation (AUC,
  Brier), a versioned model registry, and every prediction persisted for
  Phase 4 error analysis. Training is guarded by `ML_MIN_TRAINING_ROWS`.

Key endpoints: `/api/analytics/wallets/top`, `/api/wallets/{address}/stats`
(+`/history`), `/api/analytics/strategies`, `/api/analytics/patterns`,
`/api/analytics/models`.

## Roadmap

- **Phase 3**: decision engine & copy trading (evidence-gated, paper-trading
  default, strict safety limits)
- **Phase 4**: continuous self-evaluation & market regime learning
- **Phase 5**: production platform (Next.js dashboard, auth, Telegram,
  WebSocket feeds)

> Research/analytics software. Nothing here is financial advice; memecoin
> trading carries extreme risk.
