"""Model artifact registry: joblib files on disk + MlModel rows in the DB.

``save_model`` writes ``<name>_v<version>.joblib`` under the configured model
directory and inserts a new MlModel row (append-only; versions only grow).
The new version becomes active — deactivating prior versions — only when its
ROC-AUC beats the currently active one; otherwise the row is still recorded
for lineage but the old champion keeps serving.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MlModel
from app.logging_config import get_logger

log = get_logger(__name__)


async def save_model(
    session: AsyncSession,
    *,
    name: str,
    estimator: Any,
    algo: str,
    model_dir: str,
    training_rows: int,
    params: dict[str, Any],
    metrics: dict[str, Any],
    feature_names: list[str],
    trained_at: datetime | None = None,
) -> MlModel:
    """Persist the artifact, insert its MlModel row, and settle activation."""
    trained_at = trained_at or datetime.now(tz=UTC)
    version = (
        await session.execute(
            select(func.coalesce(func.max(MlModel.version), 0)).where(MlModel.name == name)
        )
    ).scalar_one() + 1

    directory = Path(model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / f"{name}_v{version}.joblib"
    # Atomic write: a crash mid-dump must never leave a truncated artifact
    # behind a committed registry row.
    tmp_path = artifact_path.with_suffix(".joblib.tmp")
    joblib.dump(estimator, tmp_path)
    tmp_path.replace(artifact_path)

    active = (
        await session.execute(
            select(MlModel).where(MlModel.name == name, MlModel.is_active.is_(True))
        )
    ).scalar_one_or_none()
    # Presence-checked promotion: a missing metric must never coerce to 0.0
    # and silently dethrone (or shield) a champion.
    new_auc = metrics.get("roc_auc")
    old_auc = (active.metrics or {}).get("roc_auc") if active is not None else None
    promote = active is None or (
        new_auc is not None and (old_auc is None or float(new_auc) > float(old_auc))
    )

    if promote:
        await session.execute(
            update(MlModel)
            .where(MlModel.name == name, MlModel.is_active.is_(True))
            .values(is_active=False)
        )

    row = MlModel(
        name=name,
        version=version,
        algo=algo,
        trained_at=trained_at,
        training_rows=training_rows,
        params=params,
        metrics=metrics,
        feature_names=feature_names,
        artifact_path=str(artifact_path),
        is_active=promote,
    )
    session.add(row)
    await session.commit()
    log.info(
        "ml_model_saved",
        name=name,
        version=version,
        promoted=promote,
        roc_auc=new_auc,
        prev_auc=old_auc,
    )
    return row


async def load_active(
    session: AsyncSession, name: str
) -> tuple[MlModel, Any, list[str]] | None:
    """Load the active model for ``name``: (row, fitted estimator, feature names)."""
    row = (
        await session.execute(
            select(MlModel).where(MlModel.name == name, MlModel.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if row is None or row.artifact_path is None:
        return None
    path = Path(row.artifact_path)
    if not path.exists():
        log.warning("ml_artifact_missing", name=name, path=str(path))
        return None
    try:
        estimator = joblib.load(path)
    except Exception as exc:
        # Truncated/corrupt file or sklearn version skew: degrade to "no
        # model" instead of crashing every prediction call site.
        log.error("ml_artifact_unloadable", name=name, path=str(path), error=str(exc))
        return None
    return row, estimator, list(row.feature_names or [])
