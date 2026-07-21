"""AI report generator: structured JSON sections + rendered markdown.

Each report is one append-only :class:`Report` row covering a trailing window.
It is organized into named SECTIONS, each a dict of
``{"conclusion", "evidence", "items"}`` where ``evidence`` is a dict of the
numbers that justify the conclusion and every item carries its own supporting
figures. Nothing here asserts a conclusion without the numbers behind it.

Data sources (all read-only):

* :class:`WalletStats` — current per-wallet metrics: best/worst wallets by
  ``confidence_score``, highest-risk (``roi_std`` / ``max_drawdown_pct``) and
  most-consistent (low ``roi_std``) wallets.
* :class:`StrategyStat` — per-style windowed performance: latest row vs the
  average of prior rows per style yields rising (best) vs declining strategies.
* :class:`ModelPerformance` — latest row per model for prediction accuracy, and
  latest-vs-prior AUC for the biggest model improvements.
* :class:`PredictionOutcome` — resolved predictions that were confident but
  wrong (high ``predicted_prob``, ``actual_label`` 0): the biggest mistakes.
* :class:`MarketRegime` — the latest regime label and its active flags.

Every section is always present; when a section has no supporting data it is
emitted with an "insufficient data" note rather than omitted, so the report's
shape is stable and it never crashes on sparse inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MarketRegime,
    ModelPerformance,
    PredictionOutcome,
    Report,
    StrategyStat,
    WalletStats,
)
from app.db.util import aware as _aware
from app.db.util import sql_cutoff, to_float
from app.logging_config import get_logger

log = get_logger(__name__)

# A wallet needs at least this many closed positions before its risk /
# consistency / worst-of rankings carry meaning; below it the metrics are noise.
MIN_CLOSED_POSITIONS = 5

INSUFFICIENT = "insufficient data"

# Regime modifier flags surfaced in the market summary, in report order.
REGIME_FLAGS: tuple[str, ...] = (
    "high_volatility",
    "low_liquidity",
    "whale_accumulation",
    "panic_selling",
    "launch_wave",
    "trend_exhaustion",
)


def _now(now: datetime | None) -> datetime:
    return _aware(now) if now is not None else datetime.now(tz=UTC)


def _section(conclusion: str, evidence: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"conclusion": conclusion, "evidence": evidence, "items": items}


def _empty(what: str) -> dict[str, Any]:
    return _section(f"{INSUFFICIENT}: {what}", {}, [])


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


# --------------------------------------------------------------------------
# Wallet-driven sections (WalletStats)
# --------------------------------------------------------------------------


def _wallet_item(stat: WalletStats) -> dict[str, Any]:
    return {
        "wallet_id": stat.wallet_id,
        "confidence_score": to_float(stat.confidence_score),
        "closed_position_count": stat.closed_position_count,
        "win_rate": to_float(stat.win_rate),
        "roi_std": to_float(stat.roi_std),
        "max_drawdown_pct": to_float(stat.max_drawdown_pct),
        "avg_roi": to_float(stat.avg_roi),
    }


async def _all_wallet_stats(session: AsyncSession) -> list[WalletStats]:
    return list((await session.execute(select(WalletStats))).scalars().all())


def _best_wallets(stats: list[WalletStats], top_n: int) -> dict[str, Any]:
    scored = [s for s in stats if s.confidence_score is not None]
    if not scored:
        return _empty("no wallets with a confidence score")
    scored.sort(key=lambda s: (to_float(s.confidence_score) or 0.0, s.wallet_id), reverse=True)
    top = scored[:top_n]
    items = [_wallet_item(s) for s in top]
    best, worst = items[0], items[-1]
    return _section(
        conclusion=(
            f"Top {len(top)} wallets by confidence lead from "
            f"{best['confidence_score']:.2f} down to {worst['confidence_score']:.2f}; "
            f"the leader won {_pct(best['win_rate'])} over "
            f"{best['closed_position_count']} closed positions."
        ),
        evidence={
            "count": len(top),
            "max_confidence": best["confidence_score"],
            "min_confidence": worst["confidence_score"],
        },
        items=items,
    )


def _worst_wallets(stats: list[WalletStats], top_n: int) -> dict[str, Any]:
    eligible = [
        s
        for s in stats
        if s.confidence_score is not None
        and (s.closed_position_count or 0) >= MIN_CLOSED_POSITIONS
    ]
    if not eligible:
        return _empty(
            f"no scored wallets with >= {MIN_CLOSED_POSITIONS} closed positions"
        )
    eligible.sort(key=lambda s: (to_float(s.confidence_score) or 0.0, s.wallet_id))
    bottom = eligible[:top_n]
    items = [_wallet_item(s) for s in bottom]
    return _section(
        conclusion=(
            f"Bottom {len(bottom)} wallets (>= {MIN_CLOSED_POSITIONS} closed positions) "
            f"score from {items[0]['confidence_score']:.2f} up to "
            f"{items[-1]['confidence_score']:.2f}; the weakest won "
            f"{_pct(items[0]['win_rate'])}."
        ),
        evidence={
            "count": len(bottom),
            "min_confidence": items[0]["confidence_score"],
            "min_closed_positions": MIN_CLOSED_POSITIONS,
        },
        items=items,
    )


def _highest_risk_wallets(stats: list[WalletStats], top_n: int) -> dict[str, Any]:
    def risk(s: WalletStats) -> float:
        return max(to_float(s.roi_std) or 0.0, to_float(s.max_drawdown_pct) or 0.0)

    eligible = [
        s
        for s in stats
        if (s.closed_position_count or 0) >= MIN_CLOSED_POSITIONS
        and (s.roi_std is not None or s.max_drawdown_pct is not None)
    ]
    if not eligible:
        return _empty("no wallets with enough positions and a risk metric")
    eligible.sort(key=lambda s: (risk(s), s.wallet_id), reverse=True)
    top = eligible[:top_n]
    items = [_wallet_item(s) for s in top]
    lead = items[0]
    return _section(
        conclusion=(
            f"Highest-risk wallet {lead['wallet_id']} shows roi_std "
            f"{_num(lead['roi_std'])} and max drawdown {_pct(lead['max_drawdown_pct'])} "
            f"over {lead['closed_position_count']} closed positions."
        ),
        evidence={
            "count": len(top),
            "max_roi_std": max((to_float(s.roi_std) or 0.0 for s in top), default=0.0),
            "max_drawdown_pct": max(
                (to_float(s.max_drawdown_pct) or 0.0 for s in top), default=0.0
            ),
        },
        items=items,
    )


def _most_consistent_wallets(stats: list[WalletStats], top_n: int) -> dict[str, Any]:
    eligible = [
        s
        for s in stats
        if s.roi_std is not None
        and (s.closed_position_count or 0) >= MIN_CLOSED_POSITIONS
    ]
    if not eligible:
        return _empty("no wallets with enough positions and a roi_std")
    eligible.sort(key=lambda s: (to_float(s.roi_std) or 0.0, s.wallet_id))
    top = eligible[:top_n]
    items = [_wallet_item(s) for s in top]
    lead = items[0]
    return _section(
        conclusion=(
            f"Most-consistent wallet {lead['wallet_id']} holds roi_std "
            f"{_num(lead['roi_std'])} across {lead['closed_position_count']} closed "
            f"positions with avg ROI {_num(lead['avg_roi'])}."
        ),
        evidence={
            "count": len(top),
            "min_roi_std": to_float(top[0].roi_std),
            "max_roi_std": to_float(top[-1].roi_std),
        },
        items=items,
    )


# --------------------------------------------------------------------------
# Strategy sections (StrategyStat)
# --------------------------------------------------------------------------


def _strategy_trends(stats: list[StrategyStat]) -> list[dict[str, Any]]:
    """Per style: latest row vs the mean of prior rows -> ROI / win-rate delta."""
    by_style: dict[str, list[StrategyStat]] = {}
    for s in stats:
        by_style.setdefault(s.style, []).append(s)

    trends: list[dict[str, Any]] = []
    for style, rows in by_style.items():
        rows.sort(key=lambda r: _aware(r.ts))
        latest = rows[-1]
        prior = rows[:-1]
        latest_roi = to_float(latest.avg_roi)
        latest_wr = to_float(latest.win_rate)
        prior_rois = [f for r in prior if (f := to_float(r.avg_roi)) is not None]
        prior_wrs = [f for r in prior if (f := to_float(r.win_rate)) is not None]
        prior_roi = sum(prior_rois) / len(prior_rois) if prior_rois else None
        prior_wr = sum(prior_wrs) / len(prior_wrs) if prior_wrs else None
        delta_roi = (
            latest_roi - prior_roi
            if latest_roi is not None and prior_roi is not None
            else None
        )
        delta_wr = (
            latest_wr - prior_wr
            if latest_wr is not None and prior_wr is not None
            else None
        )
        trends.append(
            {
                "style": style,
                "latest_avg_roi": latest_roi,
                "prior_avg_roi": prior_roi,
                "delta_avg_roi": delta_roi,
                "latest_win_rate": latest_wr,
                "prior_win_rate": prior_wr,
                "delta_win_rate": delta_wr,
                "closed_positions": latest.closed_positions,
                "prior_windows": len(prior),
            }
        )
    return trends


def _best_strategies(trends: list[dict[str, Any]]) -> dict[str, Any]:
    rising = [t for t in trends if t["delta_avg_roi"] is not None and t["delta_avg_roi"] > 0]
    if not rising:
        return _empty("no strategy improved vs its prior window")
    rising.sort(key=lambda t: t["delta_avg_roi"], reverse=True)
    lead = rising[0]
    return _section(
        conclusion=(
            f"Strategy '{lead['style']}' is rising fastest: avg ROI moved from "
            f"{_num(lead['prior_avg_roi'])} to {_num(lead['latest_avg_roi'])} "
            f"(delta {lead['delta_avg_roi']:+.4f}) over {lead['closed_positions']} "
            f"closed positions."
        ),
        evidence={"count": len(rising), "max_delta_avg_roi": lead["delta_avg_roi"]},
        items=rising,
    )


def _declining_strategies(trends: list[dict[str, Any]]) -> dict[str, Any]:
    declining = [
        t for t in trends if t["delta_avg_roi"] is not None and t["delta_avg_roi"] < 0
    ]
    if not declining:
        return _empty("no strategy declined vs its prior window")
    declining.sort(key=lambda t: t["delta_avg_roi"])
    lead = declining[0]
    return _section(
        conclusion=(
            f"Strategy '{lead['style']}' is declining fastest: avg ROI fell from "
            f"{_num(lead['prior_avg_roi'])} to {_num(lead['latest_avg_roi'])} "
            f"(delta {lead['delta_avg_roi']:+.4f})."
        ),
        evidence={"count": len(declining), "min_delta_avg_roi": lead["delta_avg_roi"]},
        items=declining,
    )


# --------------------------------------------------------------------------
# Model sections (ModelPerformance, PredictionOutcome)
# --------------------------------------------------------------------------


def _by_model_latest(
    perfs: list[ModelPerformance],
) -> dict[int, list[ModelPerformance]]:
    by_model: dict[int, list[ModelPerformance]] = {}
    for p in perfs:
        by_model.setdefault(p.model_id, []).append(p)
    for rows in by_model.values():
        rows.sort(key=lambda r: _aware(r.ts))
    return by_model


def _prediction_accuracy(perfs: list[ModelPerformance]) -> dict[str, Any]:
    if not perfs:
        return _empty("no model performance rows")
    by_model = _by_model_latest(perfs)
    items: list[dict[str, Any]] = []
    for model_id, rows in sorted(by_model.items()):
        latest = rows[-1]
        items.append(
            {
                "model_id": model_id,
                "model_name": latest.model_name,
                "auc": to_float(latest.auc),
                "brier": to_float(latest.brier),
                "accuracy": to_float(latest.accuracy),
                "base_rate": to_float(latest.base_rate),
                "resolved_count": latest.resolved_count,
            }
        )
    items.sort(key=lambda it: (it["auc"] is not None, it["auc"] or 0.0), reverse=True)
    lead = items[0]
    return _section(
        conclusion=(
            f"Model '{lead['model_name']}' leads on accuracy with AUC "
            f"{_num(lead['auc'], 3)}, Brier {_num(lead['brier'], 4)}, accuracy "
            f"{_pct(lead['accuracy'])} over {lead['resolved_count']} resolved "
            f"predictions."
        ),
        evidence={
            "model_count": len(items),
            "best_auc": lead["auc"],
            "best_accuracy": lead["accuracy"],
        },
        items=items,
    )


def _biggest_improvements(perfs: list[ModelPerformance]) -> dict[str, Any]:
    by_model = _by_model_latest(perfs)
    items: list[dict[str, Any]] = []
    for model_id, rows in by_model.items():
        if len(rows) < 2:
            continue
        latest_auc = to_float(rows[-1].auc)
        prev_auc = to_float(rows[-2].auc)
        if latest_auc is None or prev_auc is None:
            continue
        delta = latest_auc - prev_auc
        if delta <= 0:
            continue
        items.append(
            {
                "model_id": model_id,
                "model_name": rows[-1].model_name,
                "prev_auc": prev_auc,
                "latest_auc": latest_auc,
                "delta_auc": delta,
                "resolved_count": rows[-1].resolved_count,
            }
        )
    if not items:
        return _empty("no model improved its AUC vs the prior row")
    items.sort(key=lambda it: it["delta_auc"], reverse=True)
    lead = items[0]
    return _section(
        conclusion=(
            f"Model '{lead['model_name']}' improved most: AUC rose from "
            f"{_num(lead['prev_auc'], 3)} to {_num(lead['latest_auc'], 3)} "
            f"(delta {lead['delta_auc']:+.3f})."
        ),
        evidence={"count": len(items), "max_delta_auc": lead["delta_auc"]},
        items=items,
    )


# A "mistake" is a CONFIDENT wrong call: the model predicted profit
# (>= 0.5) yet the trade lost. Low-confidence wrong calls are correct
# skepticism, not mistakes.
_MISTAKE_MIN_PROB = 0.5


async def _biggest_mistakes(
    session: AsyncSession, window_start: datetime, top_n: int
) -> dict[str, Any]:
    outcomes = (
        (
            await session.execute(
                select(PredictionOutcome).where(
                    PredictionOutcome.actual_label == 0,
                    PredictionOutcome.predicted_prob.is_not(None),
                    # Bound the scan to the window in SQL (indexed resolved_at)
                    # instead of loading all history and filtering in Python.
                    PredictionOutcome.resolved_at >= sql_cutoff(session, window_start),
                )
            )
        )
        .scalars()
        .all()
    )
    confident_wrong = [
        o
        for o in outcomes
        if _aware(o.resolved_at) >= window_start
        and (to_float(o.predicted_prob) or 0.0) >= _MISTAKE_MIN_PROB
    ]
    if not confident_wrong:
        return _empty("no confident-but-wrong resolved predictions in window")
    confident_wrong.sort(
        key=lambda o: (to_float(o.predicted_prob) or 0.0, o.prediction_id), reverse=True
    )
    top = confident_wrong[:top_n]
    items = [
        {
            "prediction_id": o.prediction_id,
            "model_id": o.model_id,
            "predicted_prob": to_float(o.predicted_prob),
            "actual_label": o.actual_label,
            "actual_roi": to_float(o.actual_roi),
        }
        for o in top
    ]
    lead = items[0]
    return _section(
        conclusion=(
            f"The most confident wrong call scored p={_num(lead['predicted_prob'], 4)} "
            f"yet lost (actual ROI {_num(lead['actual_roi'])}); "
            f"{len(items)} such high-confidence misses in the window."
        ),
        evidence={
            "count": len(items),
            "max_predicted_prob": lead["predicted_prob"],
        },
        items=items,
    )


# --------------------------------------------------------------------------
# Market section (MarketRegime)
# --------------------------------------------------------------------------


async def _market_summary(session: AsyncSession) -> dict[str, Any]:
    regime = (
        await session.execute(
            select(MarketRegime).order_by(MarketRegime.ts.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if regime is None:
        return _empty("no market regime recorded")
    active = [flag for flag in REGIME_FLAGS if getattr(regime, flag)]
    item = {
        "regime": regime.regime,
        "ts": _aware(regime.ts).isoformat(),
        "window_minutes": regime.window_minutes,
        "active_flags": active,
        "description": regime.description,
    }
    flag_txt = ", ".join(active) if active else "no modifier flags"
    return _section(
        conclusion=(
            f"Latest market regime is '{regime.regime}' ({flag_txt}) over a "
            f"{regime.window_minutes}-minute window."
        ),
        evidence={"regime": regime.regime, "active_flag_count": len(active)},
        items=[item],
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

SECTION_TITLES: list[tuple[str, str]] = [
    ("best_wallets", "Best Wallets"),
    ("worst_wallets", "Worst Wallets"),
    ("best_strategies", "Best Strategies"),
    ("declining_strategies", "Declining Strategies"),
    ("highest_risk_wallets", "Highest-Risk Wallets"),
    ("most_consistent_wallets", "Most Consistent Wallets"),
    ("prediction_accuracy", "Prediction Accuracy"),
    ("biggest_mistakes", "Biggest Mistakes"),
    ("biggest_improvements", "Biggest Improvements"),
    ("market_summary", "Market Summary"),
]


def _item_bullet(key: str, item: dict[str, Any]) -> str:
    if key in ("best_wallets", "worst_wallets", "highest_risk_wallets", "most_consistent_wallets"):
        return (
            f"- wallet {item['wallet_id']}: confidence {_num(item['confidence_score'], 2)}, "
            f"win rate {_pct(item['win_rate'])}, closed {item['closed_position_count']}, "
            f"roi_std {_num(item['roi_std'])}, max drawdown {_pct(item['max_drawdown_pct'])}"
        )
    if key in ("best_strategies", "declining_strategies"):
        return (
            f"- {item['style']}: avg ROI {_num(item['prior_avg_roi'])} -> "
            f"{_num(item['latest_avg_roi'])} (delta {_num(item['delta_avg_roi'])}), "
            f"win rate {_pct(item['latest_win_rate'])}, closed {item['closed_positions']}"
        )
    if key == "prediction_accuracy":
        return (
            f"- {item['model_name']}: AUC {_num(item['auc'], 3)}, Brier "
            f"{_num(item['brier'], 4)}, accuracy {_pct(item['accuracy'])}, "
            f"n={item['resolved_count']}"
        )
    if key == "biggest_improvements":
        return (
            f"- {item['model_name']}: AUC {_num(item['prev_auc'], 3)} -> "
            f"{_num(item['latest_auc'], 3)} (delta {_num(item['delta_auc'], 3)})"
        )
    if key == "biggest_mistakes":
        return (
            f"- prediction {item['prediction_id']} (model {item['model_id']}): "
            f"p={_num(item['predicted_prob'], 4)}, actual ROI {_num(item['actual_roi'])}"
        )
    if key == "market_summary":
        flags = ", ".join(item["active_flags"]) if item["active_flags"] else "none"
        return f"- regime {item['regime']}, flags: {flags}, window {item['window_minutes']}m"
    return f"- {item}"


def _render_markdown(
    kind: str,
    window_start: datetime,
    window_end: datetime,
    summary: str,
    sections: dict[str, dict[str, Any]],
) -> str:
    lines: list[str] = [
        f"# {kind.capitalize()} Report",
        "",
        f"Window: {window_start.isoformat()} to {window_end.isoformat()}",
        "",
        "## Summary",
        "",
        summary,
        "",
    ]
    for key, title in SECTION_TITLES:
        section = sections[key]
        lines.append(f"## {title}")
        lines.append("")
        lines.append(section["conclusion"])
        lines.append("")
        for item in section["items"]:
            lines.append(_item_bullet(key, item))
        if not section["items"]:
            lines.append("- (no items)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_summary(
    kind: str, window_days: int, sections: dict[str, dict[str, Any]]
) -> str:
    best = sections["best_wallets"]
    strat = sections["best_strategies"]
    acc = sections["prediction_accuracy"]
    market = sections["market_summary"]
    mistakes = sections["biggest_mistakes"]
    n_best = best["evidence"].get("count", 0)
    top_conf = best["evidence"].get("max_confidence")
    regime = market["evidence"].get("regime", "unknown")
    best_auc = acc["evidence"].get("best_auc")
    n_mistakes = mistakes["evidence"].get("count", 0)
    return (
        f"This {kind} report covers the trailing {window_days} days. "
        f"It ranks {n_best} top wallets "
        f"(peak confidence {_num(top_conf, 2) if top_conf is not None else 'n/a'}), "
        f"tracks rising and declining strategies ({strat['conclusion']}), "
        f"reports model accuracy (best AUC "
        f"{_num(best_auc, 3) if best_auc is not None else 'n/a'}) alongside "
        f"{n_mistakes} confident-but-wrong predictions, and notes the latest "
        f"market regime is '{regime}'."
    )


async def weekly_report_exists(session: AsyncSession, now: datetime) -> bool:
    """True if a weekly report was already generated in the current ISO week.

    Lets the scheduler stay stateless while still emitting exactly one weekly
    report per week regardless of interval or restarts.
    """
    now = _aware(now)
    week_start = _aware(
        datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    )
    row = (
        await session.execute(
            select(Report.id).where(
                Report.kind == "weekly",
                Report.generated_at >= sql_cutoff(session, week_start),
            )
        )
    ).first()
    return row is not None


async def daily_report_exists(session: AsyncSession, now: datetime) -> bool:
    """True if a daily report was already generated this UTC day.

    Mirrors :func:`weekly_report_exists` — without it, every worker restart
    inside the same day emitted another duplicate daily report.
    """
    now = _aware(now)
    day_start = _aware(datetime(now.year, now.month, now.day, tzinfo=UTC))
    row = (
        await session.execute(
            select(Report.id).where(
                Report.kind == "daily",
                Report.generated_at >= sql_cutoff(session, day_start),
            )
        )
    ).first()
    return row is not None


async def generate_report(
    session: AsyncSession,
    *,
    kind: str,
    window_days: int,
    top_n: int,
    now: datetime | None = None,
) -> Report:
    """Build and persist one :class:`Report` over the trailing window.

    Returns the inserted, refreshed :class:`Report`. Every section is present
    even when its data source is empty (an "insufficient data" note), so the
    call never raises on sparse inputs.
    """
    generated_at = _now(now)
    window_start = generated_at - timedelta(days=window_days)

    wallet_stats = await _all_wallet_stats(session)
    strategy_stats = list((await session.execute(select(StrategyStat))).scalars().all())
    model_perfs = list(
        (await session.execute(select(ModelPerformance))).scalars().all()
    )
    trends = _strategy_trends(strategy_stats)

    sections: dict[str, dict[str, Any]] = {
        "best_wallets": _best_wallets(wallet_stats, top_n),
        "worst_wallets": _worst_wallets(wallet_stats, top_n),
        "best_strategies": _best_strategies(trends),
        "declining_strategies": _declining_strategies(trends),
        "highest_risk_wallets": _highest_risk_wallets(wallet_stats, top_n),
        "most_consistent_wallets": _most_consistent_wallets(wallet_stats, top_n),
        "prediction_accuracy": _prediction_accuracy(model_perfs),
        "biggest_mistakes": await _biggest_mistakes(session, window_start, top_n),
        "biggest_improvements": _biggest_improvements(model_perfs),
        "market_summary": await _market_summary(session),
    }

    summary = _build_summary(kind, window_days, sections)
    markdown = _render_markdown(kind, window_start, generated_at, summary, sections)

    report = Report(
        kind=kind,
        generated_at=generated_at,
        window_start=window_start,
        window_end=generated_at,
        summary=summary,
        sections=sections,
        markdown=markdown,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    log.info(
        "report_generated",
        kind=kind,
        window_days=window_days,
        report_id=report.id,
        sections=len(sections),
    )
    return report
