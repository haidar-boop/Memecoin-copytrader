import { getToken } from "./auth";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

function buildUrl(path: string, query?: Query): string {
  const url = new URL(path.replace(/^\//, ""), API_BASE.replace(/\/?$/, "/"));
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
}

async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      cache: "no-store",
    });
  } catch (e) {
    throw new ApiError(0, `Network error contacting API: ${String(e)}`);
  }

  if (!res.ok) {
    let detail: unknown = undefined;
    try {
      detail = await res.json();
    } catch {
      /* ignore */
    }
    const msg =
      (detail && typeof detail === "object" && "detail" in detail
        ? String((detail as { detail: unknown }).detail)
        : res.statusText) || `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, detail);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, { query }),
  post: <T>(path: string, body?: unknown, query?: Query) =>
    request<T>("POST", path, { body, query }),
  del: <T>(path: string, query?: Query) => request<T>("DELETE", path, { query }),
};

export function trackWallet(address: string): Promise<Wallet> {
  return api.post<Wallet>("/api/wallets/track", { address });
}

export function untrackWallet(address: string): Promise<Wallet> {
  return api.del<Wallet>(`/api/wallets/${address}/track`);
}

// ---- Typed response shapes (subset of backend pydantic models) ----

export interface Count {
  count: number;
  estimate: boolean;
}
export interface IngestionStats {
  counts: Record<string, Count>;
  queue_depth: number | null;
  listener_checkpoint: Record<string, string>;
}

export interface Wallet {
  id: number;
  address: string;
  first_seen_at: string;
  last_seen_at: string;
  is_tracked: boolean;
  label: string | null;
  sol_balance_lamports: number | null;
}

export interface TopWallet {
  address: string;
  wallet_id: number;
  is_tracked: boolean;
  confidence_score: number | null;
  total_pnl_sol: number | null;
  win_rate: number | null;
  pnl_30d_sol: number | null;
  closed_position_count: number;
  style: string | null;
  computed_at: string;
}

export interface WalletStats {
  wallet_id: number;
  computed_at: string;
  trade_count: number;
  buy_count: number;
  sell_count: number;
  position_count: number;
  closed_position_count: number;
  win_count: number;
  win_rate: number | null;
  total_pnl_sol: number | null;
  total_volume_sol: number | null;
  avg_roi: number | null;
  median_roi: number | null;
  profit_factor: number | null;
  max_drawdown_sol: number | null;
  max_drawdown_pct: number | null;
  avg_hold_seconds: number | null;
  median_hold_seconds: number | null;
  avg_position_sol: number | null;
  max_position_sol: number | null;
  trades_per_day: number | null;
  pnl_7d_sol: number | null;
  pnl_30d_sol: number | null;
  first_trade_at: string | null;
  last_trade_at: string | null;
  confidence_score: number | null;
  confidence_components: Array<Record<string, unknown>> | null;
  style: string | null;
  style_confidence: number | null;
}

export interface WalletStatsSnapshot {
  wallet_id: number;
  ts: string;
  trade_count: number;
  closed_position_count: number;
  win_rate: number | null;
  total_pnl_sol: number | null;
  pnl_7d_sol: number | null;
  pnl_30d_sol: number | null;
  confidence_score: number | null;
  style: string | null;
}

export interface Trade {
  signature: string;
  event_index: number;
  block_time: string;
  slot: number;
  wallet_address: string;
  token_mint: string;
  dex: string;
  aggregator: string | null;
  side: string;
  token_amount: number;
  quote_amount: number;
  quote_mint: string;
  price_quote: number | null;
  price_usd: number | null;
}

export interface Token {
  id: number;
  mint: string;
  symbol: string | null;
  name: string | null;
  decimals: number | null;
  supply: number | null;
  primary_dex: string | null;
  first_seen_at: string;
}

export interface StrategyStat {
  ts: string;
  style: string;
  window_days: number;
  wallet_count: number;
  closed_positions: number;
  win_rate: number | null;
  avg_roi: number | null;
  total_pnl_sol: number | null;
  profit_factor: number | null;
  avg_hold_seconds: number | null;
}

export interface StrategyCluster {
  id: number;
  computed_at: string;
  name: string;
  member_count: number;
  feature_names: string[] | null;
  centroid: number[] | null;
  description: string | null;
  latest_stat: StrategyStat | null;
}

export interface MlModel {
  id: number;
  name: string;
  version: number;
  algo: string;
  trained_at: string;
  training_rows: number;
  params: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  feature_names: string[] | null;
  is_active: boolean;
}

export interface Decision {
  id: number;
  created_at: string;
  source_signature: string | null;
  leader_wallet_id: number | null;
  token_id: number | null;
  side: string;
  mode: string;
  confidence_score: number | null;
  risk_score: number | null;
  expected_reward: number | null;
  expected_drawdown: number | null;
  p_profit: number | null;
  decision: string;
  size_sol: number | null;
  reasons: string[] | null;
  factors: Array<Record<string, unknown>> | null;
}

export interface CopyPosition {
  id: number;
  token_id: number;
  leader_wallet_id: number | null;
  mode: string;
  status: string;
  opened_at: string;
  closed_at: string | null;
  spent_sol: number;
  tokens_bought: number;
  sold_sol: number;
  realized_pnl_sol: number | null;
}

export interface CopyStatus {
  emergency_stop: string | null;
  daily_realized_pnl_sol: string;
  open_positions: number;
  exposure_sol: string;
  mode: string;
  enabled: boolean;
}

export interface Report {
  id: number;
  kind: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  summary: string | null;
  sections: Record<string, unknown> | null;
  markdown: string | null;
}

export interface ModelPerformance {
  id: number;
  ts: string;
  model_id: number;
  model_name: string;
  window_days: number;
  resolved_count: number;
  auc: number | null;
  brier: number | null;
  accuracy: number | null;
  base_rate: number | null;
  mean_roi_error: number | null;
  calibration: Array<Record<string, unknown>> | null;
}

export interface MarketRegime {
  id: number;
  ts: string;
  window_minutes: number;
  regime: string;
  high_volatility: boolean;
  low_liquidity: boolean;
  whale_accumulation: boolean;
  panic_selling: boolean;
  launch_wave: boolean;
  trend_exhaustion: boolean;
  features: Record<string, unknown> | null;
  description: string | null;
}

export interface NotificationItem {
  [key: string]: unknown;
  kind?: string;
  title?: string;
  message?: string;
  ts?: string;
}
