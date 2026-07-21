"""Entry point: evaluation & reporting worker (python -m workers.run_evaluation)."""

from __future__ import annotations

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.evaluation.runner import run_evaluation_loop
from workers.base import run_worker


async def main(settings: Settings) -> None:
    engine = build_engine(settings)
    try:
        await run_evaluation_loop(settings, build_session_factory(engine))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run_worker("evaluation", main)
