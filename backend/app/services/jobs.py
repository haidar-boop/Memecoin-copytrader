"""Shared periodic-job machinery for worker loops (enrichment, analytics).

A job spec is ``(name, interval_seconds, cycle)``. ``run_jobs`` starts one
task per spec with staggered first runs, counts every cycle in the
``JOB_RUNS`` metric, survives cycle failures, and tears down cleanly on
cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.logging_config import get_logger
from app.services import metrics

log = get_logger(__name__)

JobCycle = Callable[[], Awaitable[None]]
JobSpec = tuple[str, float, JobCycle]

STAGGER_SECONDS = 3.0


async def job_loop(name: str, interval_seconds: float, initial_delay: float, cycle: JobCycle) -> None:
    """Run ``cycle`` forever, ``interval_seconds`` apart, surviving failures."""
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            await cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.JOB_RUNS.labels(job=name, status="error").inc()
            log.exception("job_cycle_failed", job=name)
        else:
            metrics.JOB_RUNS.labels(job=name, status="ok").inc()
        await asyncio.sleep(interval_seconds)


async def run_jobs(loop_name: str, specs: list[JobSpec]) -> None:
    """Run every job spec until cancelled; clean teardown on cancellation."""
    tasks = [
        asyncio.create_task(
            job_loop(name, interval, min(index * STAGGER_SECONDS, interval), cycle),
            name=f"{loop_name}:{name}",
        )
        for index, (name, interval, cycle) in enumerate(specs)
    ]
    log.info("job_loop_started", loop=loop_name, jobs=[name for name, _, _ in specs])
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("job_loop_stopping", loop=loop_name)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("job_loop_stopped", loop=loop_name)
